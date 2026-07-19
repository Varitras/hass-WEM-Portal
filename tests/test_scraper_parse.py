"""Parsing tests for the web scraper's expert page.

`parse_expert_page` turns the portal's HTML into sensor dicts, so a change
in that HTML (or a regression in the parsing) shows up here as wrong values
rather than as a crash - the failure mode this module is most exposed to.

The HTML below is synthetic: it reproduces the element/class structure the
XPaths in scraper.py select, not a captured portal page (no account data,
no installation-specific ids in the repository). That covers the parsing
LOGIC; it does not prove the real portal still emits this structure.
"""

import time
import types

import pytest

from custom_components.wemportal.scraper import WemPortalScraper


def _panel(header, rows, value_cell_class="simpleDataValueCell"):
    """Build one RadPanelBar panel with the given (name, value) rows.

    Mirrors the structure parse_expert_page() selects: a header cell in a
    `simpleDataHeaderTextCell` th, and rows of name/value cells inside a
    `rpTemplate` > `simpleDataTable` table.
    """
    row_html = "".join(
        f"""
        <tr>
          <td class="simpleDataNameCell"><span>{name}</span></td>
          <td class="{value_cell_class}"><span>{value}</span></td>
        </tr>"""
        for name, value in rows
    )
    return f"""
    <div class="RadPanelBar RadPanelBar_Default rpbSimpleData">
      <table><thead><tr>
        <th class="simpleDataHeaderTextCell"><span>{header}</span></th>
      </tr></thead></table>
      <div class="rpTemplate">
        <table class="simpleDataTable"><tbody>{row_html}</tbody></table>
      </div>
    </div>"""


def _page(*panels):
    return f"<html><body>{''.join(panels)}</body></html>"


@pytest.fixture
def scraper():
    """A scraper instance without any network use.

    parse_expert_page() only reads self.session.cookies (to hand the cookie
    jar back to the caller), so the real constructor is fine here - it
    creates a session but never sends a request.
    """
    return WemPortalScraper("user@example.org", "secret")


def _parse(scraper, html):
    result = scraper.parse_expert_page(html)
    assert isinstance(result, list) and len(result) == 1
    data = result[0]
    # The cookie jar is appended under a reserved key, not a sensor.
    assert "cookie" in data
    return data


def test_value_and_unit_are_split(scraper):
    data = _parse(scraper, _page(_panel("Heat pump", [("Outside temperature", "12.5 °C")])))

    sensor = data["heat_pump-outside_temperature"]
    assert sensor["value"] == 12.5
    assert sensor["unit"] == "°C"
    assert sensor["platform"] == "sensor"
    assert sensor["icon"] is None, "a unit with a device class must not force an icon"
    assert sensor["friendlyName"] == "Heat pump - Outside temperature"


def test_german_decimal_comma_becomes_a_float(scraper):
    """The portal writes "21,5" in German. Parsed as a string this would
    reach Home Assistant as a non-numeric state."""
    data = _parse(scraper, _page(_panel("Heating circuit", [("Room temperature", "21,5 °C")])))

    assert data["heating_circuit-room_temperature"]["value"] == 21.5


def test_unit_is_derived_from_the_name_when_absent(scraper):
    """A bare number carries no unit; the keyword lists in const.py are the
    fallback (temperature -> °C, speed/power request -> %)."""
    page = _page(
        _panel("Heat pump", [("Temperatur Vorlauf", "34")]),
        _panel("Pump", [("Drehzahl", "80")]),
    )
    data = _parse(scraper, page)

    assert data["heat_pump-temperatur_vorlauf"]["unit"] == "°C"
    assert data["pump-drehzahl"]["unit"] == "%"


def test_non_numeric_value_keeps_the_full_string_and_no_unit(scraper):
    """A status text must not be torn apart at the first space - the whole
    string is the value, and it has no unit."""
    data = _parse(scraper, _page(_panel("Status", [("Mode", "Reduziert Betrieb")])))

    sensor = data["status-mode"]
    assert sensor["value"] == "Reduziert Betrieb"
    assert sensor["unit"] is None


def test_boolean_and_missing_values_are_sanitized(scraper):
    """Shared sanitize_value(): on/off become numbers, "--" becomes None -
    a missing reading must not surface as a fabricated 0."""
    page = _page(
        _panel("Pump", [("Ein", "Ein"), ("Aus", "Aus"), ("Missing", "--")])
    )
    data = _parse(scraper, page)

    assert data["pump-ein"]["value"] == 1.0
    assert data["pump-aus"]["value"] == 0.0
    assert data["pump-missing"]["value"] is None


def test_enum_value_cells_are_parsed_too(scraper):
    """Enum values live in a differently-classed cell; both are selected."""
    data = _parse(
        scraper,
        _page(_panel("Mode", [("Operating mode", "Automatik")],
                     value_cell_class="simpleDataValueEnumCell")),
    )

    assert data["mode-operating_mode"]["value"] == "Automatik"


def test_panel_without_a_header_is_skipped(scraper):
    """Without a header there is no stable sensor name, so the whole panel
    is dropped instead of producing unnamed entities."""
    page = """
    <html><body>
      <div class="RadPanelBar RadPanelBar_Default rpbSimpleData">
        <div class="rpTemplate">
          <table class="simpleDataTable"><tbody>
            <tr>
              <td class="simpleDataNameCell"><span>Orphan</span></td>
              <td class="simpleDataValueCell"><span>1 °C</span></td>
            </tr>
          </tbody></table>
        </div>
      </div>
    </body></html>"""

    data = _parse(scraper, page)

    assert [key for key in data if key != "cookie"] == []


def test_rows_of_several_panels_do_not_collide(scraper):
    """The header is part of the key, so the same parameter name under two
    modules stays two distinct sensors."""
    page = _page(
        _panel("Heating circuit 1", [("Temperatur", "30 °C")]),
        _panel("Heating circuit 2", [("Temperatur", "40 °C")]),
    )
    data = _parse(scraper, page)

    assert data["heating_circuit_1-temperatur"]["value"] == 30.0
    assert data["heating_circuit_2-temperatur"]["value"] == 40.0


def test_expert_module_page_prefers_the_postback_response():
    """The module select is an async postback whose response already carries
    the re-rendered module panel. Discarding it and issuing a separate
    `GET Default.aspx` returned no parameters in practice, so the postback
    response is used when it has editable rows - and no second request is
    sent."""
    from custom_components.wemportal import expert_writer

    delta = _delta_with_parameter("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    client._postback = lambda *a, **k: delta
    client.session = _ExplodingSession()

    html_content = client._fetch_module_page({"index": 6, "label": "Heat pump"})

    assert html_content is delta


def test_expert_module_page_falls_back_to_the_plain_get():
    """If the postback response carries no rows, the previous behaviour is
    kept rather than giving up."""
    from custom_components.wemportal import expert_writer

    page = _delta_with_parameter("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    client = expert_writer.WemPortalExpertClient("user@example.org", "secret")
    client._postback = lambda *a, **k: "<html><body>no panel here</body></html>"
    client.session = _StubSession(page)

    html_content = client._fetch_module_page({"index": 6, "label": "Heat pump"})

    assert html_content == page


def test_session_cache_round_trip_survives_a_new_client():
    """The point of the cache is that the NEXT operation skips the login.

    Three mutations of `_save_session` survived the suite before this test:
    the cache could be silently disabled and everything stayed green.
    """
    from custom_components.wemportal import expert_writer

    jar = {}
    first = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar=jar
    )
    first.session = _CookieSession({"ASP.NET_SessionId": "session-one"})
    first._save_session()

    assert jar["cookies"] == {"ASP.NET_SessionId": "session-one"}

    # A later, independent client must restore exactly those cookies.
    restored = {}
    second = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar=jar
    )
    second._full_login = lambda: _fail("cached session must be reused")
    second._establish_context = lambda: restored.update(second.session.cookies)

    second._login()

    assert restored == {"ASP.NET_SessionId": "session-one"}


class _CookieSession:
    """Minimal stand-in for a curl_cffi session's cookie jar."""

    def __init__(self, cookies):
        self.cookies = dict(cookies)

    def close(self):
        pass


def _client_with_jar(jar):
    from custom_components.wemportal import expert_writer

    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar=jar
    )
    client._full_login = lambda: _fail("a full login should not have happened")
    return client


def _fail(message):
    raise AssertionError(message)


def test_expert_session_is_reused_instead_of_logging_in():
    """Every expert operation used to perform a full login, and the portal
    rejected exactly that (403 on Login.aspx) while the cookie-reusing
    scraper kept working. A young cached session must be continued."""

    jar = {"cookies": {"ASP.NET_SessionId": "abc"}, "saved_at": time.monotonic() - 100}
    before = jar["saved_at"]
    client = _client_with_jar(jar)
    client._establish_context = lambda: None

    client._login()

    # `saved_at >= 0` proved nothing - monotonic() is always >= 0 and the
    # fixture had just set it. Assert the cache was really refreshed.
    assert jar["saved_at"] > before, "session cache was not refreshed"


def test_expired_cache_logs_in_again():
    """Past the age cap we do not spend two requests on a probably-dead
    session - we log in directly."""
    from custom_components.wemportal import expert_writer

    jar = {
        "cookies": {"ASP.NET_SessionId": "abc"},
        "saved_at": time.monotonic() - (expert_writer.EXPERT_SESSION_MAX_AGE_SECONDS + 10),
    }
    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar=jar
    )
    called = []
    attempts = []
    client._full_login = lambda: called.append(True)
    # NOT a raising guard: _try_cached_session wraps _establish_context in a
    # broad `except Exception`, which swallows the AssertionError and falls
    # through to the login - so a raising guard passes even when the age cap
    # is ignored entirely (verified by mutation). Count the calls instead.
    client._establish_context = lambda: attempts.append(True)

    client._login()

    assert attempts == [], "a stale session must not even be probed"
    assert called == [True]


def test_dead_session_falls_back_to_a_full_login():
    from custom_components.wemportal import expert_writer
    from custom_components.wemportal.exceptions import AuthError

    jar = {"cookies": {"ASP.NET_SessionId": "abc"}, "saved_at": time.monotonic()}
    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar=jar
    )
    called = []
    client._full_login = lambda: called.append(True)

    def dead():
        raise AuthError("session not accepted by portal main page")

    client._establish_context = dead

    client._login()

    assert called == [True]


def test_a_403_during_reuse_is_not_retried_with_a_login():
    """A rejection must reach the caller so the backoff engages. Retrying
    with a full login would only add a second rejected request - and the
    login is the request the portal rejects most readily."""
    from custom_components.wemportal.exceptions import ForbiddenError

    jar = {"cookies": {"ASP.NET_SessionId": "abc"}, "saved_at": time.monotonic()}
    client = _client_with_jar(jar)

    def rejected():
        raise ForbiddenError("403 on the main page")

    client._establish_context = rejected

    with pytest.raises(ForbiddenError):
        client._login()


def test_empty_cache_goes_straight_to_login():
    from custom_components.wemportal import expert_writer

    client = expert_writer.WemPortalExpertClient(
        "user@example.org", "secret", cookie_jar={}
    )
    called = []
    client._full_login = lambda: called.append(True)
    client._establish_context = lambda: _fail("nothing to reuse")

    client._login()

    assert called == [True]


def _delta_with_parameter(entityvalue):
    """A Telerik delta stream carrying one editable parameter row.

    Shaped like the real thing: length|type|id|<html fragment>|, i.e. the
    panel markup is embedded in a non-HTML envelope.
    """
    fragment = f"""
    <div class="RadPanelBar">
      <span id="x_HeaderTemplate_lblHeaderText">Heating</span>
      <table><tr>
        <td><span>Power limit</span></td>
        <td><span>30 %</span></td>
        <td><input class="EditIcon" type="button"
             onclick="window.open('WwpsParameterDetails.aspx?entityvalue={entityvalue}&readdata=True')"/></td>
      </tr></table>
    </div>"""
    return f"1234|updatePanel|ctl00_UpdatePanel|{fragment}|"


class _StubSession:
    def __init__(self, text):
        self._text = text

    def get(self, *_a, **_k):
        return types.SimpleNamespace(status_code=200, text=self._text, url="https://x/")


class _ExplodingSession:
    """Fails the test if a second request is made."""

    def get(self, *_a, **_k):
        raise AssertionError("no follow-up request should be sent")


def test_incomplete_row_is_skipped_without_losing_the_rest(scraper):
    """One malformed row must not cost the whole panel."""
    page = """
    <html><body>
      <div class="RadPanelBar RadPanelBar_Default rpbSimpleData">
        <table><thead><tr>
          <th class="simpleDataHeaderTextCell"><span>Heat pump</span></th>
        </tr></thead></table>
        <div class="rpTemplate">
          <table class="simpleDataTable"><tbody>
            <tr><td class="simpleDataNameCell"><span>No value</span></td></tr>
            <tr>
              <td class="simpleDataNameCell"><span>Good</span></td>
              <td class="simpleDataValueCell"><span>7 °C</span></td>
            </tr>
          </tbody></table>
        </div>
      </div>
    </body></html>"""

    data = _parse(scraper, page)

    assert "heat_pump-no_value" not in data
    assert data["heat_pump-good"]["value"] == 7.0


def test_units_without_a_device_class_keep_a_useful_icon(scraper):
    """Percent and rpm have no device class in Home Assistant, so an explicit
    icon is the only way they get a meaningful one - just not a lightning
    bolt, which is what every non-Celsius unit used to receive."""
    from custom_components.wemportal.utils import uom_to_icon

    assert uom_to_icon("%") == "mdi:percent"
    assert uom_to_icon("rpm") == "mdi:fan"
    # ...while anything Home Assistant can classify gets no icon from us.
    for unit in ("BAR", "kWh", "kW", "h", "m³/h", "°C", "K", "W"):
        assert uom_to_icon(unit) is None, unit
