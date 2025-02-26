from enum import Enum
import json
from multiprocessing.pool import ThreadPool
import re
import traceback

from cutie import select
from inventree.company import Company, ManufacturerPart, SupplierPart, SupplierPriceBreak
from inventree.part import Parameter, Part
from requests.compat import quote
from requests.exceptions import HTTPError
from thefuzz import fuzz

from .categories import setup_categories_and_parameters
from .config import CATEGORIES_CONFIG, CONFIG, get_config, get_pre_creation_hooks
from .error_helper import *
from .inventree_helpers import (create_manufacturer, get_manufacturer_part,
                                get_parameter_templates, get_part, get_supplier_part,
                                update_object_data, upload_datasheet, upload_image,
                                add_stock)
from .suppliers import search
from .suppliers.base import ApiPart

class ImportResult(Enum):
    ERROR = 0
    FAILURE = 1
    INCOMPLETE = 2
    SUCCESS = 3

    def __or__(self, other):
        return self if self.value < other.value else other

class PartImporter:
    def __init__(self, inventree_api, interactive=False, verbose=False):
        self.api = inventree_api
        self.interactive = interactive
        self.verbose = verbose
        self.dry_run = hasattr(inventree_api, "DRY_RUN")

        # preload pre_creation_hooks
        get_pre_creation_hooks()

        self.category_map, self.parameter_map = setup_categories_and_parameters(self.api)
        self.parameter_templates = get_parameter_templates(self.api)

        self.part_category_to_category = {
            category.part_category.pk: category
            for category in self.category_map.values()
        }
        self.categories = set(self.category_map.values())

    def import_part(
            self,
            search_term,
            existing_part: Part = None,
            supplier_id=None,
            only_supplier=False,
            update_stock_location=None, 
            update_stock_amount=0
        ):
        info(f"searching for {search_term} ...", end="\n")
        import_result = ImportResult.SUCCESS

        self.existing_manufacturer_part = None
        search_results = search(search_term, supplier_id, only_supplier)
        for supplier, async_results in search_results:
            info(f"searching at {supplier.name} ...")
            results, result_count = async_results.get()

            if not results:
                hint(f"no results at {supplier.name}")
                continue

            if len(results) == 1:
                api_part = results[0]
            elif self.interactive:
                prompt(f"found multiple parts at {supplier.name}, select which one to import")
                results = results[:get_config()["max_results"]]
                if result_count > len(results):
                    hint(f"found {result_count} results, only showing the first {len(results)}")
                if not (api_part := self.select_api_part(results)):
                    import_result |= ImportResult.INCOMPLETE
                    continue
            else:
                warning(f"found {result_count} parts at {supplier.name}, skipping import")
                import_result |= ImportResult.INCOMPLETE
                continue

            try:
                import_result |= self.import_supplier_part(supplier, api_part, existing_part, update_stock_location, update_stock_amount)
            except HTTPError as e:
                import_result = ImportResult.ERROR

                error_str = "'unknown HTTPError'"
                if e.args and isinstance(e.args[0], dict) and (body := e.args[0].get("body")):
                    try:
                        error_str = "\n" + "\n".join((
                            f"    {key}: {value}\n" for key, value in json.loads(body).items()
                        ))
                    except json.JSONDecodeError:
                        pass
                error(f"failed to import part with: {error_str}")

                if self.verbose:
                    error(traceback.format_exc(), prefix="FULL TRACEBACK:\n")

            if import_result == ImportResult.ERROR:
                # let the other api calls finish
                for _, other_results in search_results:
                    other_results.wait()
                return ImportResult.ERROR

        if not self.existing_manufacturer_part:
            import_result |= ImportResult.FAILURE

        return import_result

    @staticmethod
    def select_api_part(api_parts: list[ApiPart]):
        mpns = [api_part.MPN for api_part in api_parts]
        max_mpn_length = max(len(mpn) for mpn in mpns)
        mpns = [mpn.ljust(max_mpn_length) for mpn in mpns]

        manufacturers = [str(api_part.manufacturer) for api_part in api_parts]
        max_manufacturer_length = max(len(man) for man in manufacturers)
        manufacturers = [man.ljust(max_manufacturer_length) for man in manufacturers]

        skus = [api_part.SKU for api_part in api_parts]
        max_sku_length = max(len(sku) for sku in skus)
        skus = [sku.ljust(max_sku_length) for sku in skus]

        links = [f"({api_part.supplier_link})" for api_part in api_parts]

        choices = [*map(" ".join, zip(map(" | ".join, zip(mpns, manufacturers, skus)), links))]
        choices.append(f"{BOLD}Skip ...{BOLD_END}")

        index = select(choices, deselected_prefix="  ", selected_prefix="> ")
        return [*api_parts, None][index]

    def add_or_update_stock(self, part_id, stock_location_id, update_stock_amount):

        add_stock(self.api, stock_location_id, part_id, update_stock_amount)

    def import_supplier_part(self, supplier: Company, api_part: ApiPart, part: Part = None, update_stock_location=None, update_stock_amount=0):
        import_result = ImportResult.SUCCESS

        if supplier_part := get_supplier_part(self.api, api_part.SKU):
            info(f"found existing {supplier.name} part {supplier_part.SKU} ...")
        else:
            info(f"importing {supplier.name} part {api_part.SKU} ...")

        if supplier_part and supplier_part.manufacturer_part is not None:
            manufacturer_part = ManufacturerPart(self.api, supplier_part.manufacturer_part)
        elif manufacturer_part := get_manufacturer_part(self.api, api_part.MPN):
            pass
        elif self.existing_manufacturer_part:
            manufacturer_part = self.existing_manufacturer_part
        else:
            if not api_part.finalize():
                return ImportResult.FAILURE
            result = self.create_manufacturer_part(api_part, part)
            if isinstance(result, ImportResult):
                return result
            manufacturer_part, part = result

        update_part = (
            not self.existing_manufacturer_part
            or self.existing_manufacturer_part.pk != manufacturer_part.pk
        )
        if not self.dry_run:
            if not part:
                part = Part(self.api, manufacturer_part.part)
            elif part.pk != manufacturer_part.part:
                update_object_data(manufacturer_part, {"part": part.pk})

            if update_part:
                if not api_part.finalize():
                    return ImportResult.FAILURE
                update_object_data(part, api_part.get_part_data(), f"part {api_part.MPN}")

            if not part.image and api_part.image_url:
                upload_image(part, api_part.image_url)

            attachment_types = {attachment.comment for attachment in part.getAttachments()}
            if "datasheet" not in attachment_types and api_part.datasheet_url:
                match get_config().get("datasheets"):
                    case "upload":
                        upload_datasheet(part, api_part.datasheet_url)
                    case "link":
                        datasheet_url_safe = quote(api_part.datasheet_url, safe=":/")
                        part.addLinkAttachment(datasheet_url_safe[:200], comment="datasheet")
                    case None | False:
                        pass
                    case invalid_mode:
                        warning(f"invalid value 'datasheets: {invalid_mode}' in {CONFIG}")

        if api_part.parameters:
            result = self.setup_parameters(part, api_part, update_part)
            import_result |= result

        self.existing_manufacturer_part = manufacturer_part

        supplier_part_data = {
            "part": 0 if self.dry_run else part.pk,
            "manufacturer_part": manufacturer_part.pk,
            "supplier": supplier.pk,
            "SKU": api_part.SKU,
            **api_part.get_supplier_part_data(),
        }
        if supplier_part:
            action_str = "updated"
            update_object_data(supplier_part, supplier_part_data, f"{supplier.name} part")
        else:
            action_str = "added"
            supplier_part = SupplierPart.create(self.api, supplier_part_data)

        self.setup_price_breaks(supplier_part, api_part)

        url = self.api.base_url + supplier_part.url[1:]
        success(f"{action_str} {supplier.name} part {supplier_part.SKU} ({url})")

        # set stock location and amount
        if (update_stock_location):
            self.add_or_update_stock(supplier_part_data["part"], update_stock_location, update_stock_amount)

        return import_result

    def create_manufacturer_part(
        self,
        api_part: ApiPart,
        part: Part = None,
    ) -> tuple[ManufacturerPart, Part]:
        part_data = api_part.get_part_data()
        if part or (part := get_part(self.api, api_part.MPN)):
            update_object_data(part, part_data, f"part {api_part.MPN}")
        else:
            for subcategory in reversed(api_part.category_path):
                if category := self.category_map.get(subcategory):
                    break
            else:
                path_str = f" {BOLD}/{BOLD_END} ".join(api_part.category_path)
                if not self.interactive:
                    error(f"failed to match category for '{path_str}'")
                    return ImportResult.FAILURE

                prompt(f"failed to match category for '{path_str}', select category")
                if not (category := self.select_category(api_part.category_path)):
                    return ImportResult.FAILURE
                category.add_alias(api_part.category_path[-1])

            info(f"creating part {api_part.MPN} in '{category.part_category.pathstring}' ...")
            part = Part.create(self.api, {"category": category.part_category.pk, **part_data})

        manufacturer = create_manufacturer(self.api, api_part.manufacturer)
        info(f"creating manufacturer part {api_part.MPN} ...")
        manufacturer_part = ManufacturerPart.create(self.api, {
            "part": part.pk,
            "manufacturer": manufacturer.pk,
            **api_part.get_manufacturer_part_data(),
        })

        return manufacturer_part, part

    def select_category(self, category_path):
        search_terms = [category_path[-1], " ".join(category_path[-2:])]

        def rate_category(category):
            return max(
                fuzz.ratio(term, name)
                for name in (category.name, " ".join(category.path[-2:]))
                for term in search_terms
            )
        category_matches = sorted(self.categories, key=rate_category, reverse=True)

        N_MATCHES = min(5, len(category_matches))
        choices = (
            *(" / ".join(category.path) for category in category_matches[:N_MATCHES]),
            f"{BOLD}Enter Manually ...{BOLD_END}",
            f"{BOLD}Skip ...{BOLD_END}"
        )
        while True:
            index = select(choices, deselected_prefix="  ", selected_prefix="> ")
            if index == N_MATCHES + 1:
                return None
            elif index < N_MATCHES:
                return category_matches[index]

            name = prompt_input("category name")
            if (category := self.category_map.get(name)) and category.name == name:
                return category
            warning(f"category '{name}' does not exist")
            prompt("select category")

    def setup_price_breaks(self, supplier_part, api_part: ApiPart):
        price_breaks = {
            price_break.quantity: price_break
            for price_break in SupplierPriceBreak.list(self.api, part=supplier_part.pk)
        }

        updated_pricing = False
        for quantity, price in api_part.price_breaks.items():
            if price_break := price_breaks.get(quantity):
                if price == float(price_break.price):
                    continue
                price_break.save({"price": price, "price_currency": api_part.currency})
                updated_pricing = True
            else:
                SupplierPriceBreak.create(self.api, {
                    "part": supplier_part.pk,
                    "quantity": quantity,
                    "price": price,
                    "price_currency": api_part.currency,
                })
                updated_pricing = True

        if updated_pricing:
            info("updating price breaks ...")

    def setup_parameters(self, part, api_part: ApiPart, update_existing=True):
        import_result = ImportResult.SUCCESS

        if self.dry_run and not part:
            return import_result

        if not (category := self.part_category_to_category.get(part.category)):
            name = part.getCategory().pathstring
            error(f"category '{name}' is not defined in {CATEGORIES_CONFIG}")
            return ImportResult.FAILURE

        existing_parameters = {
            parameter.template_detail["name"]: parameter
            for parameter in Parameter.list(self.api, part=part.pk)
        }

        print("Available Parameters")
        for api_part_parameter, value in api_part.parameters.items():
            print(api_part_parameter, " -> " ,value)

        matched_parameters = {}
        for api_part_parameter, value in api_part.parameters.items():
            for parameter in self.parameter_map.get(api_part_parameter, []):
                name = parameter.name
                if name in category.parameters and name not in matched_parameters:
                    matched_parameters[name] = value

        already_set_parameters = {
            name for name, parameter in existing_parameters.items() if parameter.data}
        unassigned_parameters = (
            set(category.parameters) - set(matched_parameters) - already_set_parameters)

        if unassigned_parameters and self.interactive:
            prompt(f"failed to match some parameters from '{api_part.supplier_link}'", end="\n")
            for parameter_name in unassigned_parameters.copy():
                prompt(f"failed to match value for parameter '{parameter_name}', select value")
                alias, value = self.select_parameter(parameter_name, api_part.parameters)
                if value is None:
                    continue
                matched_parameters[parameter_name] = value
                unassigned_parameters.remove(parameter_name)

                if not alias:
                    continue
                if not (params := self.parameter_map.get(parameter_name)) or len(params) != 1:
                    warning(f"failed to add alias '{alias}' for parameter '{parameter_name}'")
                    continue

                parameter = params[0]
                parameter.add_alias(alias)

                if existing := self.parameter_map.get(alias):
                    existing.append(parameter)
                else:
                    self.parameter_map[alias] = [parameter]

        thread_pool = ThreadPool(4)
        async_results = []
        for name, value in matched_parameters.items():
            if not (value := sanitize_parameter_value(value)):
                continue

            if existing_parameter := existing_parameters.get(name):
                if update_existing and existing_parameter.data != value:
                    async_results.append(thread_pool.apply_async(
                        update_parameter, (existing_parameter, value)
                    ))
            else:
                if parameter_template := self.parameter_templates.get(name):
                    async_results.append(thread_pool.apply_async(
                        create_parameter, (self.api, part, parameter_template, value)
                    ))
                elif not self.dry_run:
                    warning(f"failed to find template parameter for '{name}'")
                    import_result |= ImportResult.INCOMPLETE

        if async_results:
            info("updating part parameters ...")

        for result in async_results:
            if warning_str := result.get():
                warning(warning_str)
                import_result |= ImportResult.INCOMPLETE

        if unassigned_parameters:
            plural = "s" if len(unassigned_parameters) > 1 else ""
            warning(
                f"failed to match {len(unassigned_parameters)} parameter{plural} from supplier "
                f"API ({str(unassigned_parameters)[1:-1]})"
            )
            import_result |= ImportResult.INCOMPLETE

        return import_result

    @staticmethod
    def select_parameter(parameter_name, parameters) -> tuple[str, str]:
        N_MATCHES = min(20, len(parameters))
        parameter_matches_items = sorted(
            parameters.items(),
            key=lambda item: max(fuzz.partial_ratio(parameter_name, term) for term in item),
            reverse=True
        )
        parameter_matches = dict(parameter_matches_items[:N_MATCHES])

        max_value_length = max(len(str(value)) for value in parameter_matches.values())
        values = [str(value).ljust(max_value_length) for value in parameter_matches.values()]
        names = list(parameter_matches.keys())

        choices = (
            *(f"{value} | {BOLD}{name}{BOLD_END}" for value, name in zip(values, names)),
            f"{BOLD}Enter Value Manually ...{BOLD_END}",
            f"{BOLD}Skip ...{BOLD_END}"
        )
        index = select(choices, deselected_prefix="  ", selected_prefix="> ")
        if index == N_MATCHES + 1:
            return None, None
        elif index < N_MATCHES:
            return parameter_matches_items[index]

        value = prompt_input("value")
        return None, value

def create_parameter(inventree_api, part, parameter_template, value):
    try:
        Parameter.create(inventree_api, {
            "part": part.pk,
            "template": parameter_template.pk,
            "data": value,
        })
    except HTTPError as e:
        msg = e.args[0]["body"]
        return f"failed to create parameter '{parameter_template.name}' with '{msg}'"

def update_parameter(parameter, value):
    try:
        parameter.save({"data": value})
    except HTTPError as e:
        msg = e.args[0]["body"]
        return f"failed to update parameter '{parameter.name}' to '{value}' with '{msg}'"

SANITIZE_PARAMETER = re.compile("±")

def sanitize_parameter_value(value: str) -> str:
    value = value.strip()
    if value == "-":
        return ""
    value = SANITIZE_PARAMETER.sub("", value)
    value = value.replace("Ohm", "ohm").replace("ohms", "ohm")
    return value
