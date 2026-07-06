import re
import unittest

import lookup
import route


# Independent product contract: all 39 incorporated King County cities.
KING_COUNTY_CITIES = {
    "algona", "auburn", "beaux arts village", "bellevue", "black diamond",
    "bothell", "burien", "carnation", "clyde hill", "covington",
    "des moines", "duvall", "enumclaw", "federal way", "hunts point",
    "issaquah", "kenmore", "kent", "kirkland", "lake forest park",
    "maple valley", "medina", "mercer island", "milton", "newcastle",
    "normandy park", "north bend", "pacific", "redmond", "renton",
    "sammamish", "seatac", "seattle", "shoreline", "skykomish",
    "snoqualmie", "tukwila", "woodinville", "yarrow point",
}


class CityCatalogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = route.load_routing_data()

    def test_routing_catalog_contains_exactly_39_king_county_cities(self):
        self.assertEqual(
            set(self.data["king_county_cities"]),
            KING_COUNTY_CITIES,
        )

    def test_every_city_has_a_fallback_portal(self):
        self.assertEqual(
            KING_COUNTY_CITIES - set(self.data["city_portals"]),
            set(),
        )

    def test_lookup_catalog_recognizes_every_city(self):
        lookup_cities = set(lookup.JURIS_BY_NAME) | set(lookup.SEPARATE_PORTALS)
        self.assertEqual(KING_COUNTY_CITIES - lookup_cities, set())


def _city_contract(city):
    def test(self):
        data = self.data
        address = f"123 Main St, {city.title()}, WA"

        self.assertEqual(lookup.detect_city(address), city)
        self.assertEqual(route.detect_city(address, data), city)

        building = route.route_permit("building", city, data)
        if city in data["cities_on_mbp"]:
            self.assertEqual(building["portal"], route.MBP_SEARCH)
        else:
            self.assertEqual(building["portal"], data["city_portals"][city])

        electrical = route.route_permit("electrical", city, data)
        self.assertTrue(electrical["portal"])
        if city in data["cities_own_electrical"]:
            self.assertEqual(electrical["portal"], data["city_portals"][city])
        else:
            self.assertEqual(electrical["portal"], route.LNI_PORTAL)

    return test


for _city in sorted(KING_COUNTY_CITIES):
    _test_name = "test_city_" + re.sub(r"[^a-z0-9]+", "_", _city)
    setattr(CityCatalogContractTests, _test_name, _city_contract(_city))


if __name__ == "__main__":
    unittest.main()
