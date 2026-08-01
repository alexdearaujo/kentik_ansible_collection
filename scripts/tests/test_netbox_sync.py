"""Unit and end-to-end tests for scripts/netbox_sync.py.

Run with: uv run pytest
"""

import sys
from unittest.mock import MagicMock

import pytest

import netbox_sync as ns


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestStripCidr:
    def test_strips_prefix(self):
        assert ns.strip_cidr("192.0.2.1/24") == "192.0.2.1"

    def test_passes_through_without_cidr(self):
        assert ns.strip_cidr("192.0.2.1") == "192.0.2.1"

    def test_handles_none(self):
        assert ns.strip_cidr(None) is None

    def test_handles_empty_string(self):
        assert ns.strip_cidr("") == ""


class TestNetboxAuthHeader:
    def test_v1_token_uses_token_scheme(self):
        token = "0123456789abcdef0123456789abcdef01234567"
        assert ns.netbox_auth_header(token) == f"Token {token}"

    def test_v2_token_uses_bearer_scheme(self):
        token = "nbt_4F9DAouzURLb.zjebxBPzICiPbWz0Wtx0fTL7bCKXKGTYhNzkgC2S"
        assert ns.netbox_auth_header(token) == f"Bearer {token}"


class TestNmsAgentTag:
    def test_returns_agent_id_when_tag_present(self):
        device = {"name": "rtr1", "tags": [{"name": "kentik_primary_agent=abc123"}]}
        assert ns.nms_agent_tag(device) == "abc123"

    def test_returns_none_when_tag_absent(self):
        device = {"name": "rtr1", "tags": [{"name": "site:dc1"}]}
        assert ns.nms_agent_tag(device) is None

    def test_returns_none_with_no_tags_key(self):
        assert ns.nms_agent_tag({"name": "rtr1"}) is None

    def test_ignores_tag_without_equals(self):
        device = {"tags": [{"name": "kentik_primary_agent_backup"}]}
        assert ns.nms_agent_tag(device) is None


class TestContainerPrefixesBySite:
    def test_groups_container_prefixes_by_scoped_site(self):
        prefixes = [
            {"prefix": "1.1.1.0/24", "status": {"value": "container"},
             "scope_type": "dcim.site", "scope": {"name": "DC1"}},
            {"prefix": "2.2.2.0/24", "status": {"value": "container"},
             "scope_type": "dcim.site", "scope": {"name": "DC1"}},
            {"prefix": "3.3.3.0/24", "status": {"value": "container"},
             "scope_type": "dcim.site", "scope": {"name": "DC2"}},
        ]
        assert ns.container_prefixes_by_site(prefixes) == {
            "DC1": ["1.1.1.0/24", "2.2.2.0/24"],
            "DC2": ["3.3.3.0/24"],
        }

    def test_sorts_and_dedupes_per_site(self):
        prefixes = [
            {"prefix": "9.9.9.0/24", "scope_type": "dcim.site", "scope": {"name": "DC1"}},
            {"prefix": "1.1.1.0/24", "scope_type": "dcim.site", "scope": {"name": "DC1"}},
            {"prefix": "9.9.9.0/24", "scope_type": "dcim.site", "scope": {"name": "DC1"}},
        ]
        assert ns.container_prefixes_by_site(prefixes) == {"DC1": ["1.1.1.0/24", "9.9.9.0/24"]}

    def test_skips_prefixes_not_scoped_to_a_site(self):
        prefixes = [
            {"prefix": "1.1.1.0/24", "scope_type": "dcim.region", "scope": {"name": "US"}},
            {"prefix": "2.2.2.0/24", "scope_type": None, "scope": None},
        ]
        assert ns.container_prefixes_by_site(prefixes) == {}


class TestResolveSiteName:
    def _site(self, **overrides):
        base = {
            "name": "Riverside", "slug": "riverside", "facility": "",
            "region": {"name": "West", "slug": "west"}, "group": None, "tenant": None,
        }
        base.update(overrides)
        return base

    def test_default_template_is_plain_name(self):
        assert ns.resolve_site_name(self._site(), "{name}") == "Riverside"

    def test_renders_region_and_name(self):
        assert ns.resolve_site_name(self._site(), "{region}-{name}") == "West-Riverside"

    def test_falls_back_to_name_when_referenced_region_is_null(self):
        site = self._site(region=None)
        assert ns.resolve_site_name(site, "{region}-{name}") == "Riverside"

    def test_falls_back_to_name_when_referenced_facility_is_empty_string(self):
        site = self._site(facility="")
        assert ns.resolve_site_name(site, "{facility}-{name}") == "Riverside"

    def test_does_not_fall_back_for_fields_the_template_does_not_use(self):
        # region is null, but the template never references it, so it should
        # not trigger a fallback.
        site = self._site(region=None)
        assert ns.resolve_site_name(site, "{name}") == "Riverside"

    def test_renders_group_and_tenant(self):
        site = self._site(group={"name": "Branch Offices"}, tenant={"name": "Acme"})
        assert ns.resolve_site_name(site, "{tenant}/{group}/{name}") == "Acme/Branch Offices/Riverside"

    def test_renders_slug(self):
        assert ns.resolve_site_name(self._site(), "{slug}") == "riverside"

    def test_renders_region_slug_instead_of_name(self):
        # Same input either way; only the template picks name vs. slug, so
        # switching between them is a config edit, not a code change.
        assert ns.resolve_site_name(self._site(), "{region_slug}-{name}") == "west-Riverside"

    def test_renders_group_and_tenant_slugs(self):
        site = self._site(group={"name": "Branch Offices", "slug": "branch-offices"},
                           tenant={"name": "Acme", "slug": "acme"})
        assert ns.resolve_site_name(site, "{tenant_slug}/{group_slug}/{name}") == "acme/branch-offices/Riverside"

    def test_falls_back_to_name_when_referenced_slug_variant_is_null(self):
        # region is set, but has no slug (e.g. an older NetBox record) -- the
        # slug-specific placeholder should still trigger the same fallback.
        site = self._site(region={"name": "West", "slug": None})
        assert ns.resolve_site_name(site, "{region_slug}-{name}") == "Riverside"

    def test_does_not_fall_back_when_only_the_unused_variant_is_missing(self):
        # region has a name but no slug; a template using {region} (not
        # {region_slug}) should render fine since it never looks at slug.
        site = self._site(region={"name": "West", "slug": None})
        assert ns.resolve_site_name(site, "{region}-{name}") == "West-Riverside"


class TestParseLabelSources:
    def test_default_spec_includes_all_three_by_slug(self):
        assert ns.parse_label_sources(ns.DEFAULT_LABEL_SOURCES_SPEC) == {
            "role": "slug", "tenant": "slug", "tag": "slug",
        }

    def test_drops_a_source_left_out_of_the_spec(self):
        assert ns.parse_label_sources("role:slug,tenant:slug") == {"role": "slug", "tenant": "slug"}

    def test_supports_name_field(self):
        assert ns.parse_label_sources("tenant:name") == {"tenant": "name"}

    def test_ignores_surrounding_whitespace(self):
        assert ns.parse_label_sources(" role : slug , tenant : name ") == {"role": "slug", "tenant": "name"}

    def test_ignores_empty_entries(self):
        assert ns.parse_label_sources("role:slug,,tenant:slug,") == {"role": "slug", "tenant": "slug"}

    def test_empty_spec_yields_no_sources(self):
        assert ns.parse_label_sources("") == {}

    def test_rejects_unknown_source(self):
        with pytest.raises(ValueError, match="unknown label source 'device'"):
            ns.parse_label_sources("device:slug")

    def test_rejects_unknown_field(self):
        with pytest.raises(ValueError, match="unknown label field 'id'"):
            ns.parse_label_sources("role:id")

    def test_rejects_entry_missing_a_colon(self):
        with pytest.raises(ValueError, match="invalid --label-sources entry 'role'"):
            ns.parse_label_sources("role")


class TestPrefixedLabelValue:
    def test_prefixes_role(self):
        assert ns._prefixed_label_value("role", "core") == "role:core"

    def test_prefixes_tenant(self):
        assert ns._prefixed_label_value("tenant", "Acme Corp") == "tenant:Acme Corp"

    def test_prefixes_tag(self):
        assert ns._prefixed_label_value("tag", "prod") == "tag:prod"

    def test_returns_none_for_missing_value(self):
        assert ns._prefixed_label_value("role", None) is None

    def test_returns_none_for_empty_value(self):
        assert ns._prefixed_label_value("role", "") is None


class TestFormatFailuresTable:
    def test_renders_box_drawing_table_with_header_and_rows(self):
        failures = [
            {"phase": "sites", "item": "dc1", "reason": "HTTP POST ... returned 500: boom"},
            {"phase": "devices", "item": "rtr1", "reason": "HTTP PUT ... failed after 3 retries"},
        ]
        table = ns.format_failures_table(failures)
        lines = table.splitlines()
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[-1].startswith("└") and lines[-1].endswith("┘")
        assert "Phase" in lines[1] and "Item" in lines[1] and "Reason" in lines[1]
        assert any("sites" in line and "dc1" in line for line in lines)
        assert any("devices" in line and "rtr1" in line for line in lines)
        # every row is the same width, so the table columns line up
        assert len({len(line) for line in lines}) == 1

    def test_truncates_long_reasons(self):
        failures = [{"phase": "sites", "item": "dc1", "reason": "x" * 200}]
        table = ns.format_failures_table(failures)
        assert "…" in table
        assert "x" * 200 not in table

    def test_empty_failures_still_renders_header(self):
        table = ns.format_failures_table([])
        assert "Phase" in table
        assert "Item" in table
        assert "Reason" in table


class TestFormatSummaryTable:
    def test_renders_box_drawing_table_with_header_and_rows(self):
        rows = [
            ("sites", "2.1s", "created=3 updated=1 unchanged=41 failed=0"),
            ("total", "2.1s", ""),
        ]
        table = ns.format_summary_table(rows)
        lines = table.splitlines()
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[-1].startswith("└") and lines[-1].endswith("┘")
        assert "Phase" in lines[1] and "Duration" in lines[1] and "Summary" in lines[1]
        assert any("sites" in line and "created=3" in line for line in lines)
        assert any("total" in line for line in lines)
        # every row is the same width, so the table columns line up
        assert len({len(line) for line in lines}) == 1

    def test_empty_rows_still_renders_header(self):
        table = ns.format_summary_table([])
        assert "Phase" in table
        assert "Duration" in table
        assert "Summary" in table


class TestFormatDuration:
    def test_formats_sub_minute_durations_in_seconds(self):
        assert ns._format_duration(2.1) == "2.1s"
        assert ns._format_duration(0.04) == "0.0s"
        assert ns._format_duration(59.94) == "59.9s"

    def test_formats_minute_plus_durations_as_minutes_and_seconds(self):
        assert ns._format_duration(60.0) == "1m 0.0s"
        assert ns._format_duration(84.8) == "1m 24.8s"
        assert ns._format_duration(3661.2) == "61m 1.2s"


class TestCountFailures:
    def test_counts_only_matching_phase(self):
        failures = [
            {"phase": "sites", "item": "dc1", "reason": "x"},
            {"phase": "devices", "item": "rtr1", "reason": "x"},
            {"phase": "devices", "item": "rtr2", "reason": "x"},
        ]
        assert ns._count_failures(failures, "devices") == 2
        assert ns._count_failures(failures, "sites") == 1
        assert ns._count_failures(failures, "labels: assign") == 0

    def test_empty_failures_counts_zero(self):
        assert ns._count_failures([], "devices") == 0


class TestCleanFailureReason:
    def test_extracts_message_from_kentik_json_error_body(self):
        reason = (
            'HTTP POST https://grpc.api.kentik.com/device/v202504beta2/device returned 400: '
            '{"code":3,"message":"ValidationError: Device name (pp_mdf) Already Exists '
            '(errxid d9p2c4gtkfgg0d9e77wg)","details":[]}'
        )
        cleaned = ns._clean_failure_reason(reason)
        assert cleaned == "400: ValidationError: Device name (pp_mdf) Already Exists (errxid d9p2c4gtkfgg0d9e77wg)"

    def test_falls_back_to_raw_body_when_not_json(self):
        reason = "HTTP POST https://x.test/thing returned 500: plain text error"
        assert ns._clean_failure_reason(reason) == "500: plain text error"

    def test_leaves_non_http_reasons_unchanged(self):
        reason = "Kentik plan 'Gold' not found"
        assert ns._clean_failure_reason(reason) == reason

    def test_leaves_retries_exhausted_message_unchanged(self):
        reason = "HTTP PUT https://x.test/thing failed after 3 retries"
        assert ns._clean_failure_reason(reason) == reason


class TestDeviceFieldChanges:
    def _existing(self, **overrides):
        base = {
            "deviceDescription": "Synced from NetBox",
            "deviceSubtype": "router",
            "deviceSampleRate": "1",
            "deviceBgpType": "none",
            "minimizeSnmp": False,
            "sendingIps": [],
            "deviceSnmpIp": "",
            "deviceSnmpCommunity": "",
            "site": {"id": "100"},
            "plan": {"id": "9"},
        }
        base.update(overrides)
        return base

    def _desired(self, **overrides):
        base = {
            "deviceDescription": "Synced from NetBox",
            "deviceSubtype": "router",
            "deviceSampleRate": 1,
            "planId": 9,
            "siteId": 100,
            "deviceBgpType": "none",
            "minimizeSnmp": False,
        }
        base.update(overrides)
        return base

    def test_no_changes_when_everything_matches(self):
        assert ns._device_field_changes(self._existing(), self._desired()) == {}

    def test_detects_description_change(self):
        changes = ns._device_field_changes(self._existing(deviceDescription="old"), self._desired())
        assert "deviceDescription" in changes
        assert changes["deviceDescription"] == "'old' -> 'Synced from NetBox'"

    def test_detects_site_change_via_nested_site_id(self):
        changes = ns._device_field_changes(self._existing(site={"id": "1"}), self._desired(siteId=100))
        assert changes["siteId"] == "1 -> 100"

    def test_detects_plan_change_via_nested_plan_id(self):
        changes = ns._device_field_changes(self._existing(plan={"id": "5"}), self._desired(planId=9))
        assert changes["planId"] == "5 -> 9"

    def test_ignores_fields_not_present_in_desired(self):
        # sendingIps isn't set on the desired device (no primary IP in NetBox);
        # a stale value already in Kentik shouldn't be reported as a change.
        existing = self._existing(sendingIps=["10.0.0.1"])
        desired = self._desired()
        assert "sendingIps" not in ns._device_field_changes(existing, desired)

    def test_flags_nms_as_present_without_diffing_it(self):
        changes = ns._device_field_changes(self._existing(), self._desired(nms={"agentId": "1"}))
        assert changes["nms"] == "NMS agent config included (not diffed)"


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

CONFIG_ARGV = [
    "netbox_sync.py",
    "--kentik-email", "e@x.com",
    "--kentik-token", "tok",
    "--netbox-url", "http://netbox.test",
    "--netbox-token", "nbtok",
]


class TestGetConfigPlanRequirement:
    def test_plan_required_for_default_full_run(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV)
        with pytest.raises(SystemExit, match="KENTIK_PLAN_NAME"):
            ns.get_config()

    def test_plan_required_for_only_devices(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "devices"])
        with pytest.raises(SystemExit, match="KENTIK_PLAN_NAME"):
            ns.get_config()

    def test_plan_not_required_for_only_sites(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites"])
        cfg = ns.get_config()
        assert cfg.kentik_plan is None

    def test_plan_not_required_for_only_labels(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "labels"])
        cfg = ns.get_config()
        assert cfg.kentik_plan is None


class TestGetConfigSiteNameTemplate:
    def test_defaults_to_plain_name(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites"])
        cfg = ns.get_config()
        assert cfg.site_name_template == "{name}"

    def test_accepts_a_template_using_supported_fields(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--site-name-template", "{region}-{name}"])
        cfg = ns.get_config()
        assert cfg.site_name_template == "{region}-{name}"

    def test_rejects_a_template_with_an_unknown_field(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--site-name-template", "{regio}-{name}"])
        with pytest.raises(SystemExit, match="unknown field.*regio"):
            ns.get_config()

    def test_accepts_a_slug_variant_template(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--site-name-template", "{region_slug}-{name}"])
        cfg = ns.get_config()
        assert cfg.site_name_template == "{region_slug}-{name}"


class TestGetConfigLabelSources:
    def test_defaults_to_role_tenant_tag_by_slug(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites"])
        cfg = ns.get_config()
        assert cfg.label_sources == "role:slug,tenant:slug,tag:slug"

    def test_accepts_a_custom_spec(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--label-sources", "role:slug,tenant:name"])
        cfg = ns.get_config()
        assert cfg.label_sources == "role:slug,tenant:name"

    def test_rejects_an_unknown_source(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--label-sources", "device:slug"])
        with pytest.raises(SystemExit, match="unknown label source 'device'"):
            ns.get_config()

    def test_rejects_an_unknown_field(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", CONFIG_ARGV + ["--only", "sites",
                                                          "--label-sources", "role:id"])
        with pytest.raises(SystemExit, match="unknown label field 'id'"):
            ns.get_config()


# ---------------------------------------------------------------------------
# kentik_request
# ---------------------------------------------------------------------------

class TestKentikRequest:
    def test_success_returns_response(self, monkeypatch):
        mock_resp = MagicMock(status_code=200, headers={})
        monkeypatch.setattr(ns.requests, "request", MagicMock(return_value=mock_resp))
        assert ns.kentik_request("GET", "http://x/y", {}) is mock_resp

    def test_404_returns_none(self, monkeypatch):
        mock_resp = MagicMock(status_code=404, headers={})
        monkeypatch.setattr(ns.requests, "request", MagicMock(return_value=mock_resp))
        assert ns.kentik_request("GET", "http://x/y", {}) is None

    def test_error_status_raises(self, monkeypatch):
        mock_resp = MagicMock(status_code=500, headers={}, text="boom")
        monkeypatch.setattr(ns.requests, "request", MagicMock(return_value=mock_resp))
        with pytest.raises(RuntimeError, match="500"):
            ns.kentik_request("GET", "http://x/y", {})

    def test_rate_limit_retries_then_succeeds(self, monkeypatch):
        limited = MagicMock(status_code=429, headers={"x-ratelimit-reset": "0"})
        ok = MagicMock(status_code=200, headers={})
        mock_request = MagicMock(side_effect=[limited, ok])
        monkeypatch.setattr(ns.requests, "request", mock_request)
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        assert ns.kentik_request("GET", "http://x/y", {}) is ok
        assert mock_request.call_count == 2

    def test_retries_exhausted_raises(self, monkeypatch):
        limited = MagicMock(status_code=429, headers={"x-ratelimit-reset": "0"})
        monkeypatch.setattr(ns.requests, "request", MagicMock(return_value=limited))
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        with pytest.raises(RuntimeError, match="failed after 3 retries"):
            ns.kentik_request("GET", "http://x/y", {})

    def test_low_remaining_quota_sleeps(self, monkeypatch):
        mock_resp = MagicMock(status_code=200, headers={"x-ratelimit-remaining": "3"})
        monkeypatch.setattr(ns.requests, "request", MagicMock(return_value=mock_resp))
        sleep_mock = MagicMock()
        monkeypatch.setattr(ns.time, "sleep", sleep_mock)
        ns.kentik_request("GET", "http://x/y", {})
        sleep_mock.assert_called_once_with(10)

    def test_network_error_retries_then_succeeds(self, monkeypatch):
        ok = MagicMock(status_code=200, headers={})
        mock_request = MagicMock(side_effect=[ns.requests.exceptions.ConnectionError("no route to host"), ok])
        monkeypatch.setattr(ns.requests, "request", mock_request)
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        assert ns.kentik_request("GET", "http://x/y", {}) is ok
        assert mock_request.call_count == 2

    def test_network_error_retries_exhausted_raises_clean_error(self, monkeypatch):
        mock_request = MagicMock(side_effect=ns.requests.exceptions.ConnectionError("no route to host"))
        monkeypatch.setattr(ns.requests, "request", mock_request)
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        with pytest.raises(RuntimeError, match="failed after 3 retries"):
            ns.kentik_request("GET", "http://x/y", {})
        assert mock_request.call_count == 3

    def test_network_error_backs_off_with_increasing_wait(self, monkeypatch):
        ok = MagicMock(status_code=200, headers={})
        mock_request = MagicMock(side_effect=[
            ns.requests.exceptions.ConnectionError("x"),
            ns.requests.exceptions.ConnectionError("x"),
            ok,
        ])
        monkeypatch.setattr(ns.requests, "request", mock_request)
        sleep_mock = MagicMock()
        monkeypatch.setattr(ns.time, "sleep", sleep_mock)
        ns.kentik_request("GET", "http://x/y", {})
        assert [c.args[0] for c in sleep_mock.call_args_list] == [5, 10]


# ---------------------------------------------------------------------------
# netbox_get_all
# ---------------------------------------------------------------------------

class TestNetboxGetAll:
    def test_follows_pagination(self, requests_mock):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?limit=0",
            json={"results": [{"id": 1}], "next": "http://netbox.test/api/dcim/sites/?offset=1"},
        )
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?offset=1",
            json={"results": [{"id": 2}], "next": None},
        )
        result = ns.netbox_get_all("http://netbox.test/api/dcim/sites/?limit=0", {})
        assert result == [{"id": 1}, {"id": 2}]

    def test_raises_on_error_status(self, requests_mock):
        requests_mock.get("http://netbox.test/api/dcim/sites/", status_code=500, text="oops")
        with pytest.raises(RuntimeError, match="500"):
            ns.netbox_get_all("http://netbox.test/api/dcim/sites/", {})

    def test_network_error_retries_then_succeeds(self, monkeypatch):
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"results": [{"id": 1}], "next": None}
        mock_get = MagicMock(side_effect=[ns.requests.exceptions.ConnectionError("no route to host"), ok])
        monkeypatch.setattr(ns.requests, "get", mock_get)
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        assert ns.netbox_get_all("http://netbox.test/api/x/", {}) == [{"id": 1}]
        assert mock_get.call_count == 2

    def test_network_error_retries_exhausted_raises_clean_error(self, monkeypatch):
        mock_get = MagicMock(side_effect=ns.requests.exceptions.ConnectionError("no route to host"))
        monkeypatch.setattr(ns.requests, "get", mock_get)
        monkeypatch.setattr(ns.time, "sleep", MagicMock())
        with pytest.raises(RuntimeError, match="failed after 3 retries"):
            ns.netbox_get_all("http://netbox.test/api/x/", {})
        assert mock_get.call_count == 3

    def test_defaults_to_verifying_tls(self, monkeypatch):
        captured = {}

        def fake_get(url, headers, timeout, verify):
            captured["verify"] = verify
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"results": [], "next": None}
            return resp

        monkeypatch.setattr(ns.requests, "get", fake_get)
        ns.netbox_get_all("http://netbox.test/api/x/", {})
        assert captured["verify"] is True

    def test_insecure_flag_disables_verification(self, monkeypatch):
        captured = {}

        def fake_get(url, headers, timeout, verify):
            captured["verify"] = verify
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"results": [], "next": None}
            return resp

        monkeypatch.setattr(ns.requests, "get", fake_get)
        ns.netbox_get_all("http://netbox.test/api/x/", {}, verify=False)
        assert captured["verify"] is False


class TestFetchScopedSite:
    def test_returns_the_matching_site(self, requests_mock):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=DC1&limit=0",
            json={"results": [{"id": 2, "name": "DC1"}], "next": None},
        )
        site = ns.fetch_scoped_site("http://netbox.test", {}, True, "DC1")
        assert site == {"id": 2, "name": "DC1"}

    def test_url_encodes_a_name_with_special_characters(self, requests_mock):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=D.%20S.%20Weaver%20Labs&limit=0",
            json={"results": [{"id": 22, "name": "D. S. Weaver Labs"}], "next": None},
        )
        site = ns.fetch_scoped_site("http://netbox.test", {}, True, "D. S. Weaver Labs")
        assert site["id"] == 22

    def test_raises_a_clean_error_when_not_found(self, requests_mock):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=NoSuchSite&limit=0",
            json={"results": [], "next": None},
        )
        with pytest.raises(RuntimeError, match="NetBox site 'NoSuchSite' not found"):
            ns.fetch_scoped_site("http://netbox.test", {}, True, "NoSuchSite")


# ---------------------------------------------------------------------------
# KentikClient: dry-run gating
# ---------------------------------------------------------------------------

class TestKentikClientDryRun:
    def _client(self, dry_run):
        return ns.KentikClient("e@x.com", "tok", dry_run=dry_run)

    def test_create_site_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        site_id = client.create_site("New Site")
        mock_request.assert_not_called()
        assert site_id < 0

    def test_create_site_live_calls_api(self, monkeypatch):
        client = self._client(dry_run=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"site": {"id": "42"}}
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=mock_resp))
        assert client.create_site("New Site") == "42"

    def test_dry_run_placeholder_ids_are_unique(self):
        client = self._client(dry_run=True)
        assert client.create_site("A") != client.create_site("B")

    def test_update_site_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        result = client.update_site("55", {"title": "DC1", "lat": 1.0, "lon": 2.0})
        mock_request.assert_not_called()
        assert result == "55"

    def test_update_site_live_calls_api(self, monkeypatch):
        client = self._client(dry_run=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"site": {"id": "55"}}
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=mock_resp))
        assert client.update_site("55", {"title": "DC1", "lat": 1.0, "lon": 2.0}) == "55"

    def test_update_site_does_not_mutate_caller_dict(self, monkeypatch):
        client = self._client(dry_run=True)
        monkeypatch.setattr(ns, "kentik_request", MagicMock())
        site_obj = {"title": "DC1", "lat": 1.0, "lon": 2.0}
        client.update_site("55", site_obj)
        assert "id" not in site_obj

    def test_batch_create_devices_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        created, failed = client.batch_create_devices([({"deviceName": "r1"}, None)])
        mock_request.assert_not_called()
        assert failed == set()
        assert created["r1"] < 0

    def test_batch_create_devices_dry_run_gives_each_device_a_unique_placeholder_id(self, monkeypatch):
        client = self._client(dry_run=True)
        monkeypatch.setattr(ns, "kentik_request", MagicMock())
        created, _ = client.batch_create_devices([
            ({"deviceName": "r1"}, None), ({"deviceName": "r2"}, None),
        ])
        assert created["r1"] < 0 and created["r2"] < 0
        assert created["r1"] != created["r2"]

    def test_batch_update_devices_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        updated, failed = client.batch_update_devices([({"deviceName": "r1", "id": "99"}, None)])
        mock_request.assert_not_called()
        assert failed == set()
        assert updated["r1"] == "99"

    def test_batch_create_devices_dry_run_logs_pending_site_for_placeholder_site_id(self, monkeypatch, caplog):
        client = self._client(dry_run=True)
        monkeypatch.setattr(ns, "kentik_request", MagicMock())
        with caplog.at_level("INFO"):
            client.batch_create_devices([({"deviceName": "r1", "siteId": -1}, "DC1")])
        assert "site 'DC1' is itself pending creation" in caplog.text
        assert "placeholder siteId=-1" in caplog.text

    def test_batch_create_devices_dry_run_logs_plain_site_for_real_site_id(self, monkeypatch, caplog):
        client = self._client(dry_run=True)
        monkeypatch.setattr(ns, "kentik_request", MagicMock())
        with caplog.at_level("INFO"):
            client.batch_create_devices([({"deviceName": "r1", "siteId": 500}, "DC1")])
        assert "[site 'DC1']" in caplog.text
        assert "pending creation" not in caplog.text

    def test_batch_create_devices_live_calls_api(self, monkeypatch):
        client = self._client(dry_run=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "devices": [{"deviceName": "r1", "id": "701"}],
            "failedDevices": ["r2"],
        }
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=mock_resp))
        created, failed = client.batch_create_devices([
            ({"deviceName": "r1"}, "DC1"), ({"deviceName": "r2"}, "DC1"),
        ])
        assert created == {"r1": "701"}
        assert failed == {"r2"}

    def test_batch_create_devices_live_raises_on_404(self, monkeypatch):
        client = self._client(dry_run=False)
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="batch_create"):
            client.batch_create_devices([({"deviceName": "r1"}, None)])

    def test_batch_update_devices_live_calls_api(self, monkeypatch):
        client = self._client(dry_run=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "devices": [{"deviceName": "r1", "id": "701"}],
            "failedDevices": ["702"],
        }
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=mock_resp))
        updated, failed = client.batch_update_devices([
            ({"deviceName": "r1", "id": "701"}, "DC1"), ({"deviceName": "r2", "id": "702"}, "DC1"),
        ])
        assert updated == {"r1": "701"}
        assert failed == {"702"}

    def test_batch_update_devices_live_raises_on_404(self, monkeypatch):
        client = self._client(dry_run=False)
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="batch_update"):
            client.batch_update_devices([({"deviceName": "r1", "id": "701"}, None)])

    def test_list_devices_live_calls_api(self, monkeypatch):
        client = self._client(dry_run=False)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"devices": [{"deviceName": "Rtr1", "id": "701"}]}
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=mock_resp))
        assert client.list_devices() == {"rtr1": {"deviceName": "Rtr1", "id": "701"}}

    def test_create_label_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        label_id = client.create_label("core", "#ff0000")
        mock_request.assert_not_called()
        assert label_id < 0

    def test_set_device_labels_dry_run_does_not_call_api(self, monkeypatch):
        client = self._client(dry_run=True)
        mock_request = MagicMock()
        monkeypatch.setattr(ns, "kentik_request", mock_request)
        client.set_device_labels("5", [1, 2])
        mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# KentikClient: 404 guards
# ---------------------------------------------------------------------------

class TestNoneResponseGuards:
    def test_get_sites_raises_on_404(self, monkeypatch):
        client = ns.KentikClient("e", "t")
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="sites"):
            client.get_sites()

    def test_get_labels_raises_on_404(self, monkeypatch):
        client = ns.KentikClient("e", "t")
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="labels"):
            client.get_labels()

    def test_get_plan_id_raises_on_404(self, monkeypatch):
        client = ns.KentikClient("e", "t")
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="plans"):
            client.get_plan_id("Gold")

    def test_list_devices_raises_on_404(self, monkeypatch):
        client = ns.KentikClient("e", "t")
        monkeypatch.setattr(ns, "kentik_request", MagicMock(return_value=None))
        with pytest.raises(RuntimeError, match="device"):
            client.list_devices()


# ---------------------------------------------------------------------------
# Phase orchestration: limits
# ---------------------------------------------------------------------------

class TestSyncSitesLimit:
    def test_limits_number_of_created_sites(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        netbox_sites = [{"name": f"site-{i}"} for i in range(10)]
        ns.sync_sites(kentik, netbox_sites, limit=3)
        assert kentik.ensure_site.call_count == 3

    def test_existing_sites_do_not_consume_budget(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {"site-0": {"id": "1", "lat": 0.0, "lon": 0.0}}
        netbox_sites = [{"name": "site-0"}, {"name": "site-1"}]
        ns.sync_sites(kentik, netbox_sites, limit=1)
        kentik.ensure_site.assert_called_once()

    def test_no_limit_creates_everything(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        netbox_sites = [{"name": f"site-{i}"} for i in range(5)]
        ns.sync_sites(kentik, netbox_sites, limit=None)
        assert kentik.ensure_site.call_count == 5

    def test_updates_are_capped_by_the_same_limit_as_creates(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            f"site-{i}": {"id": str(i), "lat": 0.0, "lon": 0.0} for i in range(5)
        }
        netbox_sites = [{"name": f"site-{i}", "latitude": 9.0, "longitude": 9.0} for i in range(5)]
        ns.sync_sites(kentik, netbox_sites, limit=2)
        assert kentik.update_site.call_count == 2


class TestSyncSitesLatLon:
    def test_updates_site_when_coordinates_drift(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {"DC1": {"id": "1", "lat": 10.0, "lon": 20.0}}
        netbox_sites = [{"name": "DC1", "latitude": 11.0, "longitude": 21.0}]
        result = ns.sync_sites(kentik, netbox_sites, limit=None)
        kentik.update_site.assert_called_once()
        called_id, called_obj = kentik.update_site.call_args[0]
        assert called_id == "1"
        assert called_obj["lat"] == 11.0
        assert called_obj["lon"] == 21.0
        assert result == {"DC1": "1"}

    def test_does_not_update_when_coordinates_match(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {"DC1": {"id": "1", "lat": 10.0, "lon": 20.0}}
        netbox_sites = [{"name": "DC1", "latitude": 10.0, "longitude": 20.0}]
        ns.sync_sites(kentik, netbox_sites, limit=None)
        kentik.update_site.assert_not_called()

    def test_does_not_overwrite_kentik_value_when_netbox_lacks_coordinates(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {"DC1": {"id": "1", "lat": 10.0, "lon": 20.0}}
        netbox_sites = [{"name": "DC1", "latitude": None, "longitude": None}]
        ns.sync_sites(kentik, netbox_sites, limit=None)
        kentik.update_site.assert_not_called()

    def test_update_preserves_other_fields_from_existing_site(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 10.0, "lon": 20.0, "postalAddress": {"city": "LA"}, "siteMarket": "west"}
        }
        netbox_sites = [{"name": "DC1", "latitude": 11.0, "longitude": 21.0}]
        ns.sync_sites(kentik, netbox_sites, limit=None)
        _, called_obj = kentik.update_site.call_args[0]
        assert called_obj["postalAddress"] == {"city": "LA"}
        assert called_obj["siteMarket"] == "west"


class TestSyncSitesUserAccessNetworks:
    def test_creates_site_with_container_prefixes(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        netbox_sites = [{"name": "DC1"}]
        ns.sync_sites(kentik, netbox_sites, container_prefixes_by_site={"DC1": ["1.1.1.1/32", "2.2.2.2/32"]},
                      limit=None)
        kentik.ensure_site.assert_called_once_with(
            title="DC1", lat=0.0, lon=0.0,
            user_access_networks=["1.1.1.1/32", "2.2.2.2/32"], site_cache={},
        )

    def test_updates_site_when_user_access_networks_drift(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 0.0, "lon": 0.0,
                    "addressClassification": {"userAccessNetworks": ["1.1.1.1/32"]}}
        }
        netbox_sites = [{"name": "DC1"}]
        ns.sync_sites(kentik, netbox_sites,
                      container_prefixes_by_site={"DC1": ["1.1.1.1/32", "2.2.2.2/32"]}, limit=None)
        kentik.update_site.assert_called_once()
        called_id, called_obj = kentik.update_site.call_args[0]
        assert called_id == "1"
        assert called_obj["addressClassification"]["userAccessNetworks"] == ["1.1.1.1/32", "2.2.2.2/32"]

    def test_does_not_update_when_networks_match_regardless_of_order(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 0.0, "lon": 0.0,
                    "addressClassification": {"userAccessNetworks": ["2.2.2.2/32", "1.1.1.1/32"]}}
        }
        netbox_sites = [{"name": "DC1"}]
        ns.sync_sites(kentik, netbox_sites,
                      container_prefixes_by_site={"DC1": ["1.1.1.1/32", "2.2.2.2/32"]}, limit=None)
        kentik.update_site.assert_not_called()

    def test_does_not_touch_infrastructure_or_other_networks(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 0.0, "lon": 0.0,
                    "addressClassification": {
                        "infrastructureNetworks": ["10.0.0.0/8"],
                        "userAccessNetworks": [],
                        "otherNetworks": ["172.16.0.0/12"],
                    }}
        }
        netbox_sites = [{"name": "DC1"}]
        ns.sync_sites(kentik, netbox_sites, container_prefixes_by_site={"DC1": ["1.1.1.1/32"]}, limit=None)
        _, called_obj = kentik.update_site.call_args[0]
        assert called_obj["addressClassification"]["infrastructureNetworks"] == ["10.0.0.0/8"]
        assert called_obj["addressClassification"]["otherNetworks"] == ["172.16.0.0/12"]


class TestSyncSitesNaming:
    def test_creates_site_using_rendered_title_but_returns_netbox_name_keyed_map(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.return_value = "501"
        netbox_sites = [{"name": "Riverside", "region": {"name": "West"}}]
        result = ns.sync_sites(kentik, netbox_sites, limit=None, site_name_template="{region}-{name}")
        kentik.ensure_site.assert_called_once()
        assert kentik.ensure_site.call_args.kwargs["title"] == "West-Riverside"
        assert result == {"Riverside": "501"}

    def test_matches_existing_site_by_rendered_title(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {"West-Riverside": {"id": "1", "lat": 0.0, "lon": 0.0}}
        netbox_sites = [{"name": "Riverside", "region": {"name": "West"}}]
        result = ns.sync_sites(kentik, netbox_sites, limit=None, site_name_template="{region}-{name}")
        kentik.ensure_site.assert_not_called()  # already exists under the rendered title
        assert result == {"Riverside": "1"}

    def test_falls_back_to_plain_name_when_region_missing(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.return_value = "501"
        netbox_sites = [{"name": "NoRegionSite", "region": None}]
        ns.sync_sites(kentik, netbox_sites, limit=None, site_name_template="{region}-{name}")
        assert kentik.ensure_site.call_args.kwargs["title"] == "NoRegionSite"

    def test_default_template_behaves_like_before(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.return_value = "501"
        netbox_sites = [{"name": "DC1"}]
        result = ns.sync_sites(kentik, netbox_sites, limit=None)
        assert kentik.ensure_site.call_args.kwargs["title"] == "DC1"
        assert result == {"DC1": "501"}


class TestSyncSitesFailures:
    def test_failed_create_is_recorded_and_does_not_stop_other_sites(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.side_effect = [RuntimeError("boom"), "2"]
        netbox_sites = [{"name": "bad-site"}, {"name": "good-site"}]
        failures = []
        ns.sync_sites(kentik, netbox_sites, limit=None, failures=failures)
        assert kentik.ensure_site.call_count == 2
        assert failures == [{"phase": "sites", "item": "bad-site", "reason": "boom"}]

    def test_failed_update_is_recorded_and_does_not_stop_other_sites(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 1.0, "lon": 1.0},
            "DC2": {"id": "2", "lat": 1.0, "lon": 1.0},
        }
        kentik.update_site.side_effect = RuntimeError("kentik is down")
        netbox_sites = [
            {"name": "DC1", "latitude": 9.0, "longitude": 9.0},
            {"name": "DC2", "latitude": 9.0, "longitude": 9.0},
        ]
        failures = []
        ns.sync_sites(kentik, netbox_sites, limit=None, failures=failures)
        assert kentik.update_site.call_count == 2
        assert len(failures) == 2
        assert {f["item"] for f in failures} == {"DC1", "DC2"}
        assert all(f["phase"] == "sites" and f["reason"] == "kentik is down" for f in failures)

    def test_failed_site_does_not_consume_limit_budget(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.side_effect = [RuntimeError("boom"), "2"]
        netbox_sites = [{"name": "bad-site"}, {"name": "good-site"}]
        ns.sync_sites(kentik, netbox_sites, limit=1, failures=[])
        assert kentik.ensure_site.call_count == 2

    def test_no_failures_list_needed_when_not_given(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.side_effect = RuntimeError("boom")
        # Must not raise even without a failures list passed in.
        ns.sync_sites(kentik, [{"name": "bad-site"}], limit=None)


class TestSyncSitesStats:
    def test_counts_created_updated_and_unchanged(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {
            "DC1": {"id": "1", "lat": 1.0, "lon": 1.0},  # will drift -> updated
            "DC2": {"id": "2", "lat": 9.0, "lon": 9.0},  # matches -> unchanged
        }
        kentik.ensure_site.return_value = "3"
        netbox_sites = [
            {"name": "DC1", "latitude": 9.0, "longitude": 9.0},
            {"name": "DC2", "latitude": 9.0, "longitude": 9.0},
            {"name": "DC3", "latitude": 1.0, "longitude": 1.0},  # missing -> created
        ]
        stats = {}
        ns.sync_sites(kentik, netbox_sites, limit=None, stats=stats)
        assert stats == {"created": 1, "updated": 1, "unchanged": 1}

    def test_failed_items_are_not_counted_as_created_or_updated(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.side_effect = RuntimeError("boom")
        stats = {}
        ns.sync_sites(kentik, [{"name": "bad-site"}], limit=None, failures=[], stats=stats)
        assert stats == {"created": 0, "updated": 0, "unchanged": 0}

    def test_stats_not_required_by_caller(self):
        kentik = MagicMock()
        kentik.get_sites.return_value = {}
        kentik.ensure_site.return_value = "1"
        # Must not raise even without a stats dict passed in.
        ns.sync_sites(kentik, [{"name": "DC1"}], limit=None)


class TestSyncDevicesLimit:
    def _cfg(self):
        return MagicMock(sample_rate=1, snmp_community="", snmp_credential="default")

    def test_limits_number_of_devices_submitted(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": f"rtr{i}", "site": {"name": "DC1"}} for i in range(10)]
        ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=4)
        kentik.batch_create_devices.assert_called_once()
        assert len(kentik.batch_create_devices.call_args[0][0]) == 4

    def test_devices_with_unresolved_site_do_not_consume_budget(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({"rtr1": "1"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [
            {"name": "no-site-rtr", "site": {"name": "UNKNOWN"}},
            {"name": "rtr1", "site": {"name": "DC1"}},
        ]
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=1)
        specs = kentik.batch_create_devices.call_args[0][0]
        assert len(specs) == 1
        assert specs[0][0]["deviceName"] == "rtr1"
        assert device_ids == {"rtr1": "1"}

    def test_unnamed_device_is_skipped_without_crashing(self):
        # NetBox allows name=None for non-master virtual chassis members.
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({"rtr1": "1"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [
            {"id": 42, "name": None, "site": {"name": "DC1"}},
            {"name": "rtr1", "site": {"name": "DC1"}},
        ]
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=None)
        specs = kentik.batch_create_devices.call_args[0][0]
        assert len(specs) == 1
        assert device_ids == {"rtr1": "1"}

    def test_devices_already_up_to_date_do_not_consume_budget(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr1": {
                "id": "42", "deviceDescription": "Synced from NetBox", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        kentik.batch_create_devices.return_value = ({"rtr2": "2"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "rtr1", "site": {"name": "DC1"}}, {"name": "rtr2", "site": {"name": "DC1"}}]
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=1)
        kentik.batch_update_devices.assert_not_called()
        specs = kentik.batch_create_devices.call_args[0][0]
        assert len(specs) == 1
        assert device_ids == {"rtr2": "2"}


class TestSyncDevicesBatching:
    def _cfg(self):
        return MagicMock(sample_rate=1, snmp_community="", snmp_credential="default")

    def test_chunks_creates_at_the_batch_max(self, monkeypatch):
        monkeypatch.setattr(ns, "DEVICE_BATCH_MAX", 2)
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": f"rtr{i}", "site": {"name": "DC1"}} for i in range(5)]
        ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=None)
        assert kentik.batch_create_devices.call_count == 3
        chunk_sizes = [len(call.args[0]) for call in kentik.batch_create_devices.call_args_list]
        assert chunk_sizes == [2, 2, 1]

    def test_creates_and_updates_are_sent_as_separate_batches(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr1": {
                "id": "42", "deviceDescription": "old", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        kentik.batch_create_devices.return_value = ({"rtr2": "2"}, set())
        kentik.batch_update_devices.return_value = ({"rtr1": "42"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "rtr1", "site": {"name": "DC1"}}, {"name": "rtr2", "site": {"name": "DC1"}}]
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=None)
        assert device_ids == {"rtr1": "42", "rtr2": "2"}
        create_specs = kentik.batch_create_devices.call_args[0][0]
        update_specs = kentik.batch_update_devices.call_args[0][0]
        assert [d["deviceName"] for d, _ in create_specs] == ["rtr2"]
        assert [d["deviceName"] for d, _ in update_specs] == ["rtr1"]
        assert update_specs[0][0]["id"] == "42"

    def test_update_logs_field_diff(self, caplog):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr1": {
                "id": "42", "deviceDescription": "old description", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        kentik.batch_update_devices.return_value = ({"rtr1": "42"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "rtr1", "site": {"name": "DC1"}, "description": "new description"}]

        with caplog.at_level("INFO"):
            device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=None)

        assert device_ids["rtr1"] == "42"
        assert "deviceDescription" in caplog.text
        assert "'old description' -> 'new description'" in caplog.text

    def test_device_already_matching_kentik_is_left_alone(self, caplog):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr1": {
                "id": "42", "deviceDescription": "Synced from NetBox", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "rtr1", "site": {"name": "DC1"}}]

        with caplog.at_level("INFO"):
            device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(), limit=None)

        assert "already up to date" in caplog.text
        kentik.batch_update_devices.assert_not_called()
        kentik.batch_create_devices.assert_not_called()
        assert device_ids == {}


class TestSyncDevicesStats:
    def _cfg(self):
        return MagicMock(sample_rate=1, snmp_community="", snmp_credential="default")

    def test_counts_confirmed_created_updated_and_unchanged(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr-upd": {
                "id": "42", "deviceDescription": "old", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
            "rtr-same": {
                "id": "43", "deviceDescription": "Synced from NetBox", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        kentik.batch_create_devices.return_value = ({"rtr-new": "44"}, set())
        kentik.batch_update_devices.return_value = ({"rtr-upd": "42"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [
            {"name": "rtr-new", "site": {"name": "DC1"}},
            {"name": "rtr-upd", "site": {"name": "DC1"}},
            {"name": "rtr-same", "site": {"name": "DC1"}},
        ]
        stats = {}
        ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(),
                         limit=None, stats=stats)
        assert stats == {"created": 1, "updated": 1, "unchanged": 1}

    def test_created_and_updated_count_confirmed_not_merely_submitted(self):
        # A device rejected within an otherwise-successful batch response
        # must not inflate "created"/"updated" -- those counts reflect what
        # Kentik actually confirmed, not what was merely sent.
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({"good-rtr": "2"}, {"bad-rtr"})
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "bad-rtr", "site": {"name": "DC1"}}, {"name": "good-rtr", "site": {"name": "DC1"}}]
        stats = {}
        ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(),
                         limit=None, failures=[], stats=stats)
        assert stats == {"created": 1, "updated": 0, "unchanged": 0}

    def test_stats_not_required_by_caller(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({}, set())
        site_cache = {"DC1": "100"}
        # Must not raise even without a stats dict passed in.
        ns.sync_devices(kentik, [{"name": "rtr1", "site": {"name": "DC1"}}], site_cache,
                         plan_id="7", cfg=self._cfg(), limit=None)


class TestSyncDevicesFailures:
    def _cfg(self):
        return MagicMock(sample_rate=1, snmp_community="", snmp_credential="default")

    def test_batch_http_failure_is_recorded_for_every_device_in_the_batch(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.side_effect = RuntimeError("kentik unreachable")
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "bad-rtr", "site": {"name": "DC1"}}, {"name": "good-rtr", "site": {"name": "DC1"}}]
        failures = []
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(),
                                      limit=None, failures=failures)
        assert device_ids == {}
        assert {f["item"] for f in failures} == {"bad-rtr", "good-rtr"}
        assert all(f["phase"] == "devices" and f["reason"] == "kentik unreachable" for f in failures)

    def test_device_rejected_within_a_successful_batch_is_recorded(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {}
        kentik.batch_create_devices.return_value = ({"good-rtr": "2"}, {"bad-rtr"})
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "bad-rtr", "site": {"name": "DC1"}}, {"name": "good-rtr", "site": {"name": "DC1"}}]
        failures = []
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(),
                                      limit=None, failures=failures)
        assert device_ids == {"good-rtr": "2"}
        assert "bad-rtr" not in device_ids
        assert [f["item"] for f in failures] == ["bad-rtr"]
        assert failures[0]["phase"] == "devices"

    def test_a_failed_batch_does_not_stop_the_other_batch_from_being_sent(self):
        kentik = MagicMock()
        kentik.list_devices.return_value = {
            "rtr-upd": {
                "id": "42", "deviceDescription": "old", "deviceSubtype": "router",
                "deviceSampleRate": "1", "deviceBgpType": "none", "minimizeSnmp": False,
                "sendingIps": [], "deviceSnmpIp": "", "deviceSnmpCommunity": "",
                "site": {"id": "100"}, "plan": {"id": "7"},
            },
        }
        kentik.batch_create_devices.side_effect = RuntimeError("kentik unreachable")
        kentik.batch_update_devices.return_value = ({"rtr-upd": "42"}, set())
        site_cache = {"DC1": "100"}
        netbox_devices = [{"name": "rtr-new", "site": {"name": "DC1"}}, {"name": "rtr-upd", "site": {"name": "DC1"}}]
        failures = []
        device_ids = ns.sync_devices(kentik, netbox_devices, site_cache, plan_id="7", cfg=self._cfg(),
                                      limit=None, failures=failures)
        assert device_ids == {"rtr-upd": "42"}
        assert [f["item"] for f in failures] == ["rtr-new"]


class TestSyncLabelsMixedIdTypes:
    def test_assignment_does_not_crash_when_ids_mix_str_and_int(self):
        # Realistic dry-run scenario: one desired label already exists in
        # Kentik (real string id from get_labels()), another would be newly
        # created this run (negative int placeholder id from ensure_label's
        # dry-run path). Comparing/sorting that mix must not raise TypeError.
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10", "tag:new-tag": -1}
        kentik.list_devices.return_value = {"rtr1": {"id": "1", "labels": [{"id": "10"}]}}
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None,
                    "tags": [{"slug": "new-tag", "name": "new-tag"}]}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None)
        kentik.set_device_labels.assert_called_once()
        called_id, called_labels = kentik.set_device_labels.call_args[0]
        assert called_id == "1"
        assert set(called_labels) == {"10", -1}

    def test_no_update_when_already_matching_despite_mixed_types(self):
        # Existing labels already have the same mixed-type IDs as desired
        # (e.g. a placeholder id from a still-pending dry-run site); nothing
        # should be re-sent, and comparing them must not crash either.
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": -1}
        kentik.list_devices.return_value = {"rtr1": {"id": "1", "labels": [{"id": -1}]}}
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None)
        kentik.set_device_labels.assert_not_called()

    def test_device_created_this_run_under_dry_run_has_no_current_labels(self):
        # A device created earlier in the same dry-run never actually exists
        # in Kentik, so it can't appear in list_devices()'s result; that must
        # be treated as "no labels yet" rather than crashing on a missing key.
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {}  # rtr1 doesn't exist in real Kentik
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "-1"}, limit=None)
        kentik.set_device_labels.assert_called_once_with("-1", ["10"])


class TestSyncLabelsLimit:
    def test_limits_number_of_labels_created(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        roles = [{"slug": f"role-{i}", "color": "ff0000"} for i in range(10)]
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=3)
        assert kentik.ensure_label.call_count == 3

    def test_limits_device_label_assignments(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {}
        devices = [{"name": f"rtr{i}", "role": {"slug": "core"}, "tenant": None, "tags": []}
                   for i in range(5)]
        device_ids = {f"rtr{i}": str(100 + i) for i in range(5)}
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids=device_ids, limit=2)
        assert kentik.set_device_labels.call_count == 2

    def test_devices_with_nothing_to_assign_do_not_consume_budget(self):
        kentik = MagicMock()
        devices = [{"name": "rtr0", "role": None, "tenant": None, "tags": []},
                   {"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {}
        device_ids = {"rtr0": "1", "rtr1": "2"}
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids=device_ids, limit=1)
        # rtr0 has no role/tenant/tags -> free; rtr1 has "core" -> consumes the 1 budget slot
        assert kentik.set_device_labels.call_count == 1

    def test_unnamed_device_is_skipped_without_crashing(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {}
        devices = [
            {"id": 42, "name": None, "role": {"slug": "core"}, "tenant": None, "tags": []},
            {"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []},
        ]
        device_ids = {"rtr1": "2"}
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids=device_ids, limit=None)
        assert kentik.set_device_labels.call_count == 1

    def test_resolves_device_ids_after_creating_labels_when_none_given(self):
        # --only labels passes device_ids=None since Phase 2 didn't just run.
        # Label creation must happen before the bulk device read used to
        # resolve IDs, so the log (and the underlying calls) read as one
        # coherent phase rather than device lookups preceding label sync.
        kentik = MagicMock()
        # Label already exists in Kentik; ensure_label is still called (a
        # mocked no-op here; the real impl would just no-op internally too),
        # which is enough to prove ordering without depending on the mock
        # mutating label_cache the way the real client would.
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {"rtr1": {"id": "10", "labels": []}}
        roles = [{"slug": "core", "color": "ff0000"}]
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]

        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids=None, limit=None)

        call_names = [c[0] for c in kentik.mock_calls]
        assert call_names.index("ensure_label") < call_names.index("list_devices")
        kentik.set_device_labels.assert_called_once()

    def test_still_reads_device_state_in_bulk_when_dict_already_given(self):
        # An empty dict from Phase 2 (legitimately zero devices) must not be
        # confused with None (Phase 2 didn't run): device_ids passed straight
        # through unresolved, but the single bulk device read (for current
        # label state) still happens exactly once regardless.
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        kentik.list_devices.return_value = {}
        ns.sync_labels(kentik, netbox_devices=[{"name": "rtr1"}], netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=None)
        kentik.list_devices.assert_called_once()


class TestSyncLabelsSkipAssignment:
    def test_still_creates_labels(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        roles = [{"slug": "core", "color": "ff0000"}]
        ns.sync_labels(kentik, netbox_devices=[{"name": "rtr1"}], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None, skip_assignment=True)
        kentik.ensure_label.assert_called_once()

    def test_does_not_assign_labels_to_devices(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"core": "10"}
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None, skip_assignment=True)
        kentik.list_devices.assert_not_called()
        kentik.set_device_labels.assert_not_called()

    def test_does_not_resolve_device_ids_even_when_none_given(self):
        # device_ids=None would normally trigger the bulk device read to
        # resolve IDs for assignment; skip_assignment should skip that work
        # entirely too, since it would only exist to support assignment.
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        ns.sync_labels(kentik, netbox_devices=[{"name": "rtr1"}], netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids=None, limit=None, skip_assignment=True)
        kentik.list_devices.assert_not_called()


class TestSyncLabelsFailures:
    def test_failed_label_create_is_recorded_and_does_not_stop_other_labels(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        kentik.ensure_label.side_effect = [RuntimeError("rejected"), "10"]
        roles = [{"slug": "bad-role", "color": "ff0000"}, {"slug": "good-role", "color": "00ff00"}]
        failures = []
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=None, failures=failures)
        assert kentik.ensure_label.call_count == 2
        assert failures == [{"phase": "labels: create", "item": "role:bad-role", "reason": "rejected"}]

    def test_failed_label_create_does_not_consume_limit_budget(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        kentik.ensure_label.side_effect = [RuntimeError("rejected"), "10"]
        roles = [{"slug": "bad-role", "color": "ff0000"}, {"slug": "good-role", "color": "00ff00"}]
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=1, failures=[])
        assert kentik.ensure_label.call_count == 2

    def test_failed_assignment_is_recorded_and_does_not_stop_other_devices(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {}
        kentik.set_device_labels.side_effect = [RuntimeError("kentik unreachable"), None]
        devices = [
            {"name": "bad-rtr", "role": {"slug": "core"}, "tenant": None, "tags": []},
            {"name": "good-rtr", "role": {"slug": "core"}, "tenant": None, "tags": []},
        ]
        device_ids = {"bad-rtr": "1", "good-rtr": "2"}
        failures = []
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids=device_ids, limit=None, failures=failures)
        assert kentik.set_device_labels.call_count == 2
        assert failures == [{"phase": "labels: assign", "item": "bad-rtr", "reason": "kentik unreachable"}]

    def test_failed_bulk_device_read_aborts_the_whole_assignment_step(self):
        # Unlike a single device's set_device_labels call, the bulk read that
        # backs both ID resolution and label-state comparison has no
        # per-item granularity to fail independently -- if it fails, nothing
        # about any device's current state is known, so the failure
        # propagates instead of being recorded per device.
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.side_effect = RuntimeError("kentik unreachable")
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        with pytest.raises(RuntimeError, match="kentik unreachable"):
            ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                            netbox_tags=[], device_ids={"rtr1": "1"}, limit=None)


class TestSyncLabelsSources:
    def test_default_matches_role_tenant_tag_by_slug(self):
        # label_sources=None should behave exactly like the old hardcoded
        # role/tenant/tag-by-slug pipeline.
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        roles = [{"slug": "core", "color": "ff0000"}]
        tenants = [{"slug": "acme"}]
        tags = [{"slug": "prod", "color": "00ff00"}]
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=roles, netbox_tenants=tenants,
                        netbox_tags=tags, device_ids={}, limit=None)
        created_names = {call.args[0] for call in kentik.ensure_label.call_args_list}
        assert created_names == {"role:core", "tenant:acme", "tag:prod"}

    def test_dropping_tag_source_creates_no_tag_labels(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        tags = [{"slug": "prod", "color": "00ff00"}]
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=[], netbox_tenants=[],
                        netbox_tags=tags, device_ids={}, limit=None,
                        label_sources=ns.parse_label_sources("role:slug,tenant:slug"))
        kentik.ensure_label.assert_not_called()

    def test_dropping_tag_source_never_assigns_a_tag_label(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"prod": "99"}
        kentik.list_devices.return_value = {}
        devices = [{"name": "rtr1", "role": None, "tenant": None, "tags": [{"slug": "prod"}]}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None,
                        label_sources=ns.parse_label_sources("role:slug,tenant:slug"))
        kentik.set_device_labels.assert_not_called()  # nothing desired -> free, never touched

    def test_tenant_by_name_creates_and_assigns_using_the_name(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {"tenant:acme corp": "50"}
        kentik.list_devices.return_value = {}
        devices = [{"name": "rtr1", "role": None, "tenant": {"name": "Acme Corp", "slug": "acme"}, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[{"name": "Acme Corp", "slug": "acme"}],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None,
                        label_sources=ns.parse_label_sources("tenant:name"))
        # Created using the prefixed display value...
        kentik.ensure_label.assert_called_once_with("tenant:Acme Corp", "#00ff00", kentik.get_labels.return_value)
        # ...and matched for assignment via the same case-folded key ensure_label uses.
        kentik.set_device_labels.assert_called_once_with("1", ["50"])

    def test_role_by_name_does_not_match_a_device_whose_role_name_differs_in_case(self):
        # Sanity check that matching really is case-insensitive end to end,
        # not just coincidentally working because slugs are lowercase.
        kentik = MagicMock()
        kentik.get_labels.return_value = {"role:core switch": "77"}
        kentik.list_devices.return_value = {}
        devices = [{"name": "rtr1", "role": {"name": "CORE SWITCH", "slug": "core-switch"}, "tenant": None, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None,
                        label_sources=ns.parse_label_sources("role:name"))
        kentik.set_device_labels.assert_called_once_with("1", ["77"])

    def test_empty_sources_creates_and_assigns_nothing(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        roles = [{"slug": "core", "color": "ff0000"}]
        devices = [{"name": "rtr1", "role": {"slug": "core"}, "tenant": None, "tags": []}]
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None, label_sources={})
        kentik.ensure_label.assert_not_called()
        kentik.set_device_labels.assert_not_called()


class TestSyncLabelsStats:
    def test_counts_created_and_unchanged_labels_and_assignments(self):
        kentik = MagicMock()
        # "role:core" already exists (unchanged); "tenant:acme" is new (created).
        kentik.get_labels.return_value = {"role:core": "10"}
        kentik.list_devices.return_value = {
            "rtr-set": {"id": "1", "labels": []},          # needs a label -> set
            "rtr-same": {"id": "2", "labels": [{"id": "10"}]},  # already correct -> unchanged
        }
        roles = [{"slug": "core", "color": "ff0000"}]
        tenants = [{"slug": "acme"}]
        devices = [
            {"name": "rtr-set", "role": {"slug": "core"}, "tenant": None, "tags": []},
            {"name": "rtr-same", "role": {"slug": "core"}, "tenant": None, "tags": []},
        ]
        device_ids = {"rtr-set": "1", "rtr-same": "2"}
        stats = {}
        ns.sync_labels(kentik, netbox_devices=devices, netbox_roles=roles, netbox_tenants=tenants,
                        netbox_tags=[], device_ids=device_ids, limit=None, stats=stats)
        assert stats == {"labels_created": 1, "labels_unchanged": 1,
                          "devices_assigned": 1, "devices_unchanged": 1}

    def test_skip_assignment_reports_zero_assignment_counts(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        roles = [{"slug": "core", "color": "ff0000"}]
        stats = {}
        ns.sync_labels(kentik, netbox_devices=[{"name": "rtr1"}], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={"rtr1": "1"}, limit=None,
                        skip_assignment=True, stats=stats)
        assert stats == {"labels_created": 1, "labels_unchanged": 0,
                          "devices_assigned": 0, "devices_unchanged": 0}

    def test_failed_label_create_is_not_counted_as_created_or_unchanged(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        kentik.ensure_label.side_effect = RuntimeError("rejected")
        roles = [{"slug": "bad-role", "color": "ff0000"}]
        stats = {}
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=roles, netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=None, failures=[], stats=stats)
        assert stats["labels_created"] == 0
        assert stats["labels_unchanged"] == 0

    def test_stats_not_required_by_caller(self):
        kentik = MagicMock()
        kentik.get_labels.return_value = {}
        # Must not raise even without a stats dict passed in.
        ns.sync_labels(kentik, netbox_devices=[], netbox_roles=[], netbox_tenants=[],
                        netbox_tags=[], device_ids={}, limit=None)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

BASE_ARGV = [
    "netbox_sync.py",
    "--kentik-email", "e@x.com",
    "--kentik-token", "tok",
    "--netbox-url", "http://netbox.test",
    "--netbox-token", "nbtok",
    "--kentik-plan", "MyPlan",
]

KENTIK_BASE = "https://grpc.api.kentik.com"
KENTIK_V5 = "https://api.kentik.com/api/v5"

# Path suffixes (relative to KENTIK_BASE), shared between mock registration and
# request-history assertions so the two can't silently drift apart. Sourced
# directly from netbox_sync's own constants (the single place these versions
# are defined) rather than retyped, so a version bump there doesn't silently
# leave the tests pinned to a stale endpoint.
SITES_PATH = ns.KENTIK_SITES_PATH
LABELS_PATH = ns.KENTIK_LABELS_PATH
DEVICE_PATH = ns.KENTIK_DEVICE_PATH


def device_labels_path(device_id):
    return f"{DEVICE_PATH}/{device_id}/labels"


DEVICE_BATCH_CREATE_PATH = f"{DEVICE_PATH}/batch_create"
DEVICE_BATCH_UPDATE_PATH = f"{DEVICE_PATH}/batch_update"


def _register_netbox(requests_mock, devices=None, sites=None, roles=None, tenants=None, tags=None,
                      container_prefixes=None):
    requests_mock.get("http://netbox.test/api/dcim/sites/?limit=0", json={"results": sites or [], "next": None})
    requests_mock.get("http://netbox.test/api/dcim/devices/?limit=0", json={"results": devices or [], "next": None})
    requests_mock.get("http://netbox.test/api/dcim/device-roles/?limit=0", json={"results": roles or [], "next": None})
    requests_mock.get("http://netbox.test/api/tenancy/tenants/?limit=0", json={"results": tenants or [], "next": None})
    requests_mock.get("http://netbox.test/api/extras/tags/?limit=0", json={"results": tags or [], "next": None})
    requests_mock.get("http://netbox.test/api/ipam/prefixes/?status=container&limit=0",
                       json={"results": container_prefixes or [], "next": None})


def _register_kentik_reads(requests_mock, plan_id="9", existing_devices=None):
    requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": []})
    requests_mock.get(f"{KENTIK_BASE}{LABELS_PATH}", json={"labels": []})
    requests_mock.get(f"{KENTIK_V5}/plans", json={"plans": [{"name": "MyPlan", "id": plan_id}]})
    requests_mock.get(f"{KENTIK_BASE}{DEVICE_PATH}", json={"devices": existing_devices or []})


def _make_device(name, site="DC1"):
    return {
        "name": name,
        "site": {"name": site},
        "primary_ip4": {"address": "10.0.0.1/24"},
        "role": {"slug": "core"},
        "tenant": None,
        "tags": [],
        "description": "core router",
    }


class TestRunCli:
    def test_converts_runtime_error_to_clean_exit(self, monkeypatch, caplog):
        monkeypatch.setattr(ns, "main", MagicMock(side_effect=RuntimeError("network is down")))
        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit) as exc_info:
                ns._run_cli()
        assert exc_info.value.code == 1
        assert "network is down" in caplog.text

    def test_propagates_unexpected_exceptions(self, monkeypatch):
        monkeypatch.setattr(ns, "main", MagicMock(side_effect=ValueError("this is a bug")))
        with pytest.raises(ValueError, match="this is a bug"):
            ns._run_cli()


class TestEndToEnd:
    def test_dry_run_makes_no_mutating_kentik_calls(self, requests_mock, monkeypatch):
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1")],
            sites=[{"name": "DC1", "latitude": 1.0, "longitude": 2.0}],
            roles=[{"slug": "core", "color": "ff0000"}],
        )
        _register_kentik_reads(requests_mock)

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--dry-run"])
        ns.main()

        mutating = [r for r in requests_mock.request_history
                    if r.method in ("POST", "PUT") and "kentik" in r.hostname]
        assert mutating == []

    def test_live_run_creates_expected_resources(self, requests_mock, monkeypatch):
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1")],
            sites=[{"name": "DC1", "latitude": 1.0, "longitude": 2.0}],
            roles=[{"slug": "core", "color": "ff0000"}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.post(f"{KENTIK_BASE}{SITES_PATH}", json={"site": {"id": "501"}})
        requests_mock.post(f"{KENTIK_BASE}{LABELS_PATH}", json={"label": {"id": "601"}})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV)
        ns.main()

        methods = [(r.method, r.path) for r in requests_mock.request_history]
        assert ("POST", SITES_PATH) in methods
        assert ("POST", DEVICE_BATCH_CREATE_PATH) in methods
        assert ("POST", LABELS_PATH) in methods
        assert ("PUT", device_labels_path("701")) in methods

    def test_run_prints_a_per_phase_and_total_summary_table(self, requests_mock, monkeypatch, capsys):
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1")],
            sites=[{"name": "DC1", "latitude": 1.0, "longitude": 2.0}],
            roles=[{"slug": "core", "color": "ff0000"}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.post(f"{KENTIK_BASE}{SITES_PATH}", json={"site": {"id": "501"}})
        requests_mock.post(f"{KENTIK_BASE}{LABELS_PATH}", json={"label": {"id": "601"}})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV)
        ns.main()

        out = capsys.readouterr().out
        assert "┌" in out and "└" in out
        assert "Phase" in out and "Duration" in out and "Summary" in out
        assert "sites" in out and "created=1" in out
        assert "devices" in out and "created=1" in out
        assert "labels" in out and "labels: created=1" in out and "assignments: set=1" in out
        assert "total" in out

    def test_only_sites_summary_has_no_devices_or_labels_row(self, requests_mock, monkeypatch, capsys):
        _register_netbox(requests_mock, sites=[{"name": "DC1", "latitude": 1.0, "longitude": 2.0}])
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": []})
        requests_mock.post(f"{KENTIK_BASE}{SITES_PATH}", json={"site": {"id": "501"}})

        argv = [a for a in BASE_ARGV if a not in ("--kentik-plan", "MyPlan")]
        monkeypatch.setattr(sys, "argv", argv + ["--only", "sites"])
        ns.main()

        out = capsys.readouterr().out
        lines = out.splitlines()
        assert any(line.strip().startswith("│ sites") for line in lines)
        assert not any(line.strip().startswith("│ devices") for line in lines)
        assert not any(line.strip().startswith("│ labels") for line in lines)

    def test_full_run_with_site_name_template_creates_site_and_resolves_device(self, requests_mock, monkeypatch):
        # Phase 1 creates the site under a rendered title ("West-DC1"); Phase 2
        # (running in the same invocation, right after) must still resolve
        # the device's plain NetBox site name ("DC1") against it.
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1", site="DC1")],
            sites=[{"name": "DC1", "region": {"name": "West"}}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.post(f"{KENTIK_BASE}{SITES_PATH}", json={"site": {"id": "501"}})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--site-name-template", "{region}-{name}"])
        ns.main()

        site_create = next(r for r in requests_mock.request_history if r.method == "POST" and r.path == SITES_PATH)
        assert site_create.json()["site"]["title"] == "West-DC1"

        device_create = next(r for r in requests_mock.request_history
                              if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH)
        assert device_create.json()["devices"][0]["siteId"] == 501

    def test_limit_caps_device_creation(self, requests_mock, monkeypatch):
        device_names = [f"rtr{i}" for i in range(3)]
        _register_netbox(
            requests_mock,
            devices=[_make_device(n) for n in device_names],
            sites=[{"name": "DC1"}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "1"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr0", "id": "701"}], "failedDevices": []},
        )

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--limit", "1"])
        ns.main()

        create_calls = [r for r in requests_mock.request_history
                        if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH]
        assert len(create_calls) == 1
        assert len(create_calls[0].json()["devices"]) == 1  # limit caps the batch body, not the call count

    def test_only_labels_skips_sites_and_devices_but_still_assigns_labels(self, requests_mock, monkeypatch):
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1")],
            roles=[{"slug": "core", "color": "ff0000"}],
        )
        requests_mock.get(f"{KENTIK_BASE}{LABELS_PATH}", json={"labels": []})
        requests_mock.get(f"{KENTIK_V5}/plans", json={"plans": [{"name": "MyPlan", "id": "9"}]})
        # Device already exists in Kentik; --only labels resolves this via
        # Phase 3's own bulk device read rather than Phase 2 having just run.
        requests_mock.get(
            f"{KENTIK_BASE}{DEVICE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701", "labels": []}]},
        )
        requests_mock.post(f"{KENTIK_BASE}{LABELS_PATH}", json={"label": {"id": "601"}})
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})
        # Deliberately not registering SITES_PATH or the device batch_create
        # endpoint; if --only labels touched either, requests_mock would raise
        # NoMockAddress and fail this test.

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--only", "labels"])
        ns.main()

        methods = [(r.method, r.path) for r in requests_mock.request_history]
        assert ("POST", LABELS_PATH) in methods
        assert ("PUT", device_labels_path("701")) in methods

        # Label creation must precede the bulk device read, so the log (and
        # underlying calls) read as one coherent phase.
        label_create_index = methods.index(("POST", LABELS_PATH))
        device_read_index = methods.index(("GET", DEVICE_PATH))
        assert label_create_index < device_read_index

    def test_skip_label_assignment_creates_labels_without_touching_devices(self, requests_mock, monkeypatch):
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1")],
            roles=[{"slug": "core", "color": "ff0000"}],
        )
        requests_mock.get(f"{KENTIK_BASE}{LABELS_PATH}", json={"labels": []})
        requests_mock.get(f"{KENTIK_V5}/plans", json={"plans": [{"name": "MyPlan", "id": "9"}]})
        requests_mock.post(f"{KENTIK_BASE}{LABELS_PATH}", json={"label": {"id": "601"}})
        # Deliberately not registering SITES_PATH, the device batch_create
        # endpoint, or GET DEVICE_PATH: with --skip-label-assignment, device
        # state is never read, so any of those being hit would raise
        # NoMockAddress and fail this test.

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--only", "labels", "--skip-label-assignment"])
        ns.main()

        methods = [(r.method, r.path) for r in requests_mock.request_history]
        assert ("POST", LABELS_PATH) in methods
        assert ("GET", DEVICE_PATH) not in methods

    def test_only_sites_runs_without_a_plan_name(self, requests_mock, monkeypatch):
        _register_netbox(requests_mock, sites=[{"name": "DC1", "latitude": 1.0, "longitude": 2.0}])
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": []})
        requests_mock.post(f"{KENTIK_BASE}{SITES_PATH}", json={"site": {"id": "501"}})
        # Deliberately not registering GET /plans: --only sites with no
        # --kentik-plan must never look up a plan, so NoMockAddress would
        # fail this test if it tried.

        argv = [a for a in BASE_ARGV if a not in ("--kentik-plan", "MyPlan")]
        monkeypatch.setattr(sys, "argv", argv + ["--only", "sites"])
        ns.main()

        methods = [(r.method, r.path) for r in requests_mock.request_history]
        assert ("POST", SITES_PATH) in methods

    def test_one_failed_device_does_not_abort_the_run(self, requests_mock, monkeypatch, capsys):
        device_names = ["bad-rtr", "good-rtr"]
        _register_netbox(
            requests_mock,
            devices=[_make_device(n) for n in device_names],
            sites=[{"name": "DC1"}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "1"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "good-rtr", "id": "702"}], "failedDevices": ["bad-rtr"]},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('702')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV)
        with pytest.raises(SystemExit) as exc_info:
            ns.main()
        assert exc_info.value.code == 1

        create_calls = [r for r in requests_mock.request_history
                        if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH]
        assert len(create_calls) == 1  # both devices submitted in the one batch call
        assert {d["deviceName"] for d in create_calls[0].json()["devices"]} == {"bad-rtr", "good-rtr"}

        out = capsys.readouterr().out
        assert "devices" in out
        assert "bad-rtr" in out
        assert "┌" in out and "└" in out

    def test_only_devices_resolves_site_through_the_name_template(self, requests_mock, monkeypatch):
        # NetBox device records only ever carry their site's plain name
        # ("DC1"), never its region. With a template active, the Kentik site
        # was created under a rendered title ("West-DC1"); --only devices
        # (which skips Phase 1) must still bridge "DC1" -> "West-DC1" -> id
        # to resolve the device's site correctly.
        _register_netbox(
            requests_mock,
            devices=[_make_device("rtr1", site="DC1")],
            sites=[{"name": "DC1", "region": {"name": "West"}}],
        )
        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "West-DC1", "id": "55"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "702"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('702')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--only", "devices", "--site-name-template", "{region}-{name}"])
        ns.main()

        create_calls = [r for r in requests_mock.request_history
                        if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH]
        assert len(create_calls) == 1
        assert create_calls[0].json()["devices"][0]["siteId"] == 55

    def test_label_sources_excludes_tags_and_uses_tenant_name(self, requests_mock, monkeypatch):
        device = {
            "name": "rtr1",
            "site": {"name": "DC1"},
            "primary_ip4": {"address": "10.0.0.1/24"},
            "role": {"slug": "core"},
            "tenant": {"name": "Acme Corp", "slug": "acme"},
            "tags": [{"slug": "prod", "name": "prod"}],
            "description": "core router",
        }
        _register_netbox(
            requests_mock,
            devices=[device],
            roles=[{"slug": "core", "color": "ff0000"}],
            tenants=[{"name": "Acme Corp", "slug": "acme"}],
            tags=[{"slug": "prod", "color": "00ff00"}],
        )
        requests_mock.get(f"{KENTIK_BASE}{LABELS_PATH}", json={"labels": []})
        requests_mock.get(f"{KENTIK_V5}/plans", json={"plans": [{"name": "MyPlan", "id": "9"}]})
        requests_mock.get(
            f"{KENTIK_BASE}{DEVICE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701", "labels": []}]},
        )
        requests_mock.post(f"{KENTIK_BASE}{LABELS_PATH}", json={"label": {"id": "601"}})
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--only", "labels",
                                                        "--label-sources", "role:slug,tenant:name"])
        ns.main()

        label_creates = [r.json()["label"]["name"] for r in requests_mock.request_history
                          if r.method == "POST" and r.path == LABELS_PATH]
        assert set(label_creates) == {"role:core", "tenant:Acme Corp"}  # "tag:prod" excluded: tags dropped

    def test_site_flag_scopes_fetch_and_sync_to_one_site(self, requests_mock, monkeypatch):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=DC1&limit=0",
            json={"results": [{"id": 2, "name": "DC1"}], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/dcim/devices/?site_id=2&limit=0",
            json={"results": [_make_device("rtr1", site="DC1")], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/ipam/prefixes/?status=container&site_id=2&limit=0",
            json={"results": [], "next": None},
        )
        requests_mock.get("http://netbox.test/api/dcim/device-roles/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/tenancy/tenants/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/extras/tags/?limit=0", json={"results": [], "next": None})
        # Deliberately not registering the unfiltered sites/?limit=0 or
        # devices/?limit=0 endpoints: if --site fell back to an unscoped
        # fetch, requests_mock would raise NoMockAddress and fail this test.

        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "2"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--site", "DC1"])
        ns.main()

        create_calls = [r for r in requests_mock.request_history
                        if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH]
        assert len(create_calls) == 1
        assert create_calls[0].json()["devices"][0]["deviceName"] == "rtr1"

    def test_site_flag_composes_with_only_devices(self, requests_mock, monkeypatch):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=DC1&limit=0",
            json={"results": [{"id": 2, "name": "DC1"}], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/dcim/devices/?site_id=2&limit=0",
            json={"results": [_make_device("rtr1", site="DC1")], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/ipam/prefixes/?status=container&site_id=2&limit=0",
            json={"results": [], "next": None},
        )
        requests_mock.get("http://netbox.test/api/dcim/device-roles/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/tenancy/tenants/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/extras/tags/?limit=0", json={"results": [], "next": None})

        requests_mock.get(f"{KENTIK_BASE}{DEVICE_PATH}", json={"devices": []})
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "2"}]})
        requests_mock.get(f"{KENTIK_V5}/plans", json={"plans": [{"name": "MyPlan", "id": "9"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        # Deliberately not registering SITES_PATH's POST endpoint or
        # LABELS_PATH: --only devices must never touch either.

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--site", "DC1", "--only", "devices"])
        ns.main()

        create_calls = [r for r in requests_mock.request_history
                        if r.method == "POST" and r.path == DEVICE_BATCH_CREATE_PATH]
        assert len(create_calls) == 1

    def test_site_flag_exits_cleanly_when_site_not_found(self, requests_mock, monkeypatch):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=NoSuchSite&limit=0",
            json={"results": [], "next": None},
        )
        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--site", "NoSuchSite"])
        with pytest.raises(RuntimeError, match="NetBox site 'NoSuchSite' not found"):
            ns.main()

    def test_device_fetch_requests_only_active_status(self, requests_mock, monkeypatch):
        _register_netbox(requests_mock, devices=[_make_device("rtr1")], sites=[{"name": "DC1"}])
        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "1"}]})
        requests_mock.post(
            f"{KENTIK_BASE}{DEVICE_BATCH_CREATE_PATH}",
            json={"devices": [{"deviceName": "rtr1", "id": "701"}], "failedDevices": []},
        )
        requests_mock.put(f"{KENTIK_BASE}{device_labels_path('701')}", json={})

        monkeypatch.setattr(sys, "argv", BASE_ARGV)
        ns.main()

        device_fetch = next(r for r in requests_mock.request_history
                             if r.hostname == "netbox.test" and r.path == "/api/dcim/devices/")
        assert device_fetch.qs["status"] == ["active"]

    def test_site_scoped_device_fetch_also_requests_only_active_status(self, requests_mock, monkeypatch):
        requests_mock.get(
            "http://netbox.test/api/dcim/sites/?name=DC1&limit=0",
            json={"results": [{"id": 2, "name": "DC1"}], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/dcim/devices/?status=active&site_id=2&limit=0",
            json={"results": [], "next": None},
        )
        requests_mock.get(
            "http://netbox.test/api/ipam/prefixes/?status=container&site_id=2&limit=0",
            json={"results": [], "next": None},
        )
        requests_mock.get("http://netbox.test/api/dcim/device-roles/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/tenancy/tenants/?limit=0", json={"results": [], "next": None})
        requests_mock.get("http://netbox.test/api/extras/tags/?limit=0", json={"results": [], "next": None})
        # Deliberately not registering a devices URL without status=active:
        # if the --site path dropped the status filter, requests_mock would
        # raise NoMockAddress and fail this test.
        _register_kentik_reads(requests_mock)
        requests_mock.get(f"{KENTIK_BASE}{SITES_PATH}", json={"sites": [{"title": "DC1", "id": "2"}]})

        monkeypatch.setattr(sys, "argv", BASE_ARGV + ["--site", "DC1"])
        ns.main()
