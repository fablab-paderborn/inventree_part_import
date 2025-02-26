import re
from types import MethodType

from bs4 import BeautifulSoup
from requests import Session
from requests.compat import quote, urljoin

from ..config import get_config
from ..error_helper import *
from .base import ApiPart, Supplier, money2float
from .scrape import scrape

BASE_URL = "https://reichelt.com/"
LOCALE_CHANGE_URL = f"{BASE_URL}index.html?ACTION=12&PAGE=46"
SEARCH_URL = f"{BASE_URL}index.html?ACTION=446&q={{}}"

class Reichelt(Supplier):
    def setup(self, language, location, scraping, max_results):
        if location not in LOCATION_MAP:
            error(f"failed to load '{self.name}' module (unsupported location '{location}')")
            return False

        if not scraping:
            error(f"failed to load '{self.name}' module (scraping is disabled)")
            return False

        self.language = language
        self.location = location
        self.localized_url = f"{BASE_URL}{self.location.lower()}/{self.language.lower()}/"
        self.locale_confirm_regex = re.compile(
            rf";CCOUNTRY={LOCATION_MAP[self.location]};LANGUAGE={self.language};CTYPE=1;"
        )

        self.max_results = max_results

        return True

    def search(self, search_term):
        if SKU_REGEX.fullmatch(search_term):
            sku_link = f"{self.localized_url}-{search_term}.html"
            if product_page := scrape(sku_link, setup_hook=self.setup_hook):
                product_page_soup = BeautifulSoup(product_page.content, "html.parser")
                return [self.get_api_part(product_page_soup, search_term, sku_link)], 1

        search_safe = quote(search_term, safe="")
        if not (result := scrape(SEARCH_URL.format(search_safe), setup_hook=self.setup_hook)):
            return [], 0

        search_soup = BeautifulSoup(result.content, "html.parser")

        api_parts = []
        search_results = search_soup.find_all("div", class_="al_gallery_article")
        for result in search_results[:self.max_results]:
            product_url = result.find("a", itemprop="url")["href"]
            sku = PRODUCT_URL_SKU_REGEX.match(product_url).group(1).upper()

            sku_link = f"{self.localized_url}-{sku.lower()}.html"
            if not (product_page := scrape(sku_link, setup_hook=self.setup_hook)):
                continue

            product_page_soup = BeautifulSoup(product_page.content, "html.parser")
            api_part = self.get_api_part(product_page_soup, sku, sku_link)

            if len(search_results) > 1 and search_term.lower() not in api_part.MPN.lower():
                continue

            api_parts.append(api_part)

        exact_matches = [
            api_part for api_part in api_parts
            if api_part.SKU.lower() == search_term.lower()
            or api_part.MPN.lower() == search_term.lower()
        ]
        if len(exact_matches) == 1:
            return [exact_matches[0]], 1

        n_results = len(search_results)
        return api_parts, n_results if n_results > self.max_results else len(api_parts)

    def get_api_part(self, soup, sku, link):
        description = soup.find(id="av_articleheader").find("span", itemprop="name").text

        bigimage = soup.find(id="av_bildbox").find(id="bigimages nohighlight")
        image_url = bigimage.find("img")["src"] if bigimage else None

        datasheet_url = None
        if datasheet_view := soup.find(id="av_datasheetview"):
            if datasheet := datasheet_view.find(class_="av_datasheet"):
                datasheet_url = urljoin(BASE_URL, datasheet.find("a")["href"])

        availability = soup.find("p", class_="availability").find("span")["class"][0]
        if availability not in AVAILABILITY_MAP:
            warning(f"unknown reichelt availability '{availability}' ({link})")

        breadcrumb = soup.find("ol", id="breadcrumb")
        category_path = [
            li.find("a").text
            for li in breadcrumb.find_all("li", itemprop="itemListElement")[1:]
        ]

        parameters = {
            prop_name.text.strip(): prop_value.text.strip()
            for ul in soup.find("div", id="av_props_inline").find_all("ul", class_="clearfix")
            if (prop_name := ul.find("li", "av_propname"))
            and (prop_value := ul.find("li", "av_propvalue"))
        }

        if not (manufacturer := parameters.get("Manufacturer")):
            manufacturer = "Reichelt"

        if not (mpn := parameters.get("Factory number")):
            mpn = soup.find("meta", itemprop="productID")["content"].replace(" ", "")
            if mpn.startswith("mpn:"):
                mpn = mpn[4:]

        price_breaks = {}
        if price := soup.find("meta", itemprop="price"):
            price_breaks[1] = float(price["content"].replace(",", ""))
        if discounts := soup.find(id="av_price_discount"):
            for discount in discounts.find("table").find_all("td")[1:]:
                quantity, price = discount.find_all(text=True)
                price_breaks[float(quantity)] = money2float(price.text)

        currency = None
        if meta := soup.find("meta", itemprop="priceCurrency"):
            currency = meta["content"]

        return ApiPart(
            description=description,
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=link,
            SKU=sku.upper(),
            manufacturer=manufacturer,
            manufacturer_link="",
            MPN=mpn,
            quantity_available=AVAILABILITY_MAP.get(availability),
            packaging="",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )

    def setup_hook(self, session: Session):
        request_timeout = get_config()["request_timeout"]
        form_page = session.get(LOCALE_CHANGE_URL, timeout=request_timeout)
        if form_page.status_code == 200:
            soup = BeautifulSoup(form_page.content, "html.parser")
            form_url = soup.find("form", attrs={"name": "contentform"}).attrs["action"]

            result = session.post(form_url, timeout=request_timeout, data={
                "CCOUNTRY": LOCATION_MAP[self.location],
                "LANGUAGE": self.language,
                "CTYPE": 1,
            })
            if result.status_code == 200:
                soup = BeautifulSoup(result.content, "html.parser")
                statistics = soup.find("img", width="0", height="0")
                if self.locale_confirm_regex.search(statistics.get("src", "")):
                    return

        warning("failed to set Reichelt locales")

IMAGE_URL_FULLSIZE_REGEX = re.compile(r"/resize/[^/]+/[^/]+/")
IMAGE_URL_FULLSIZE_SUB = "/images/"
SKU_REGEX = re.compile(r"^[pP]\d+$")
PRODUCT_URL_SKU_REGEX = re.compile(r"^.*([pP]\d+)\.html[^\.]*$")

# None -> available, 0 -> not available
AVAILABILITY_MAP = {
    "status_1": None,
    "status_2": 0,
    "status_3": None,
    "status_4": None,
    "status_5": 0,
    "status_6": 0,
    "status_7": None,
    "status_8": 0,
}

LOCATION_MAP = {
    "AT": 458,
    "FR": 443,
    "DE": 445,
    "IT": 446,
    "NL": 662,
    "PL": 470,
    "CH": 459,
    "US": 550,
}
