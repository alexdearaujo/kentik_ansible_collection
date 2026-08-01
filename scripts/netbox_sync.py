#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Sync NetBox devices, sites, and labels to Kentik.

Execution order:
  1. Sites  – ensure every NetBox site exists in Kentik (create if missing) with
     matching lat/lon and userAccessNetworks (see below).
  2. Devices – create or update every NetBox device in Kentik. Only devices with
     NetBox status "active" (NETBOX_DEVICE_STATUS) are ever fetched or synced.
  3. Labels  – create role/tenant/tag labels, then assign them to devices.

Site userAccessNetworks:
  A site's Kentik addressClassification.userAccessNetworks is kept in sync with
  the CIDRs of NetBox prefixes that have status "container" and are scoped
  directly to that site. Existing sites whose set of networks has drifted are
  updated alongside (or independently of) any lat/lon drift; infrastructureNetworks
  and otherNetworks are left untouched. Prefixes scoped to a region, site group,
  or location rather than a specific site are not associated with any one site
  and are skipped.

Configuration (env vars or CLI flags):
  KENTIK_EMAIL          Kentik API email                --kentik-email
  KENTIK_TOKEN          Kentik API token                --kentik-token
  NETBOX_URL            NetBox base URL                 --netbox-url
  NETBOX_TOKEN          NetBox API token (v1 or v2)     --netbox-token
  KENTIK_PLAN_NAME      Kentik plan name (required only --kentik-plan
                        when the devices phase runs)
  KENTIK_REGION         US or EU (default: US)          --kentik-region
  KENTIK_SNMP_COMMUNITY SNMP community string           --snmp-community
  KENTIK_SNMP_CRED      SNMP credential name            --snmp-credential
  KENTIK_SAMPLE_RATE    Flow sample rate (default:1)    --sample-rate
  DRY_RUN               Log planned changes only (1/0)  --dry-run
  KENTIK_SYNC_LIMIT     Cap mutations per phase         --limit
  KENTIK_SYNC_ONLY      Run a single phase              --only
  KENTIK_SKIP_LABEL_ASSIGNMENT
                        Create labels, skip assignment  --skip-label-assignment
  NETBOX_INSECURE_TLS   Skip NetBox TLS verification    --netbox-insecure-tls
  KENTIK_SITE_NAME_TEMPLATE
                        Kentik site title template      --site-name-template
                        (default: '{name}')
  KENTIK_LABEL_SOURCES  Which NetBox objects become      --label-sources
                        labels, name vs. slug (see below)
  KENTIK_SYNC_SITE      Scope sync to one NetBox site    --site
                        (exact name match)

NMS agent detection:
  If a device carries a NetBox tag named 'kentik_primary_agent=<agentId>' it is flagged
  for Kentik NMS monitoring. The <agentId> portion is used as the NMS agent ID.

NetBox authentication:
  NETBOX_TOKEN accepts either a legacy v1 token (sent as "Authorization: Token
  <token>") or a v2 token (sent as "Authorization: Bearer <token>"). The two
  are told apart automatically: v2 tokens always carry NetBox's "nbt_" prefix
  (format "nbt_<key>.<secret>"). v1 tokens are deprecated as of NetBox 4.6 and
  will be removed in NetBox 5.0.

Dry-run mode:
  With --dry-run, all read (GET) calls against NetBox and Kentik still happen so the
  script can compute an accurate plan, but every mutating call (site/label/device
  create, device update, label assignment) is logged instead of sent. Resources that
  would be newly created are tracked internally with a placeholder negative ID so the
  rest of the plan (e.g. a device referencing a site that would be newly created) can
  still be logged coherently.

Limit mode:
  --limit N caps the number of mutating operations performed *per phase* in a single
  run: at most N sites created or updated, N devices processed (created or updated),
  N new labels, and N devices whose label assignments are touched. Useful for
  smoke-testing a change against a small subset before running it against the full
  inventory.

Running a single phase:
  --only {sites,devices,labels} runs just one phase instead of the full
  sites -> devices -> labels pipeline:
    - sites:   create/update sites only.
    - devices: create/update devices only. Existing sites are still looked up
               (read-only) to resolve each device's siteId, but Phase 1's
               create/update logic does not run.
    - labels:  create labels and assign them to devices only. Since Phase 2
               did not just run, each device's Kentik ID is looked up
               individually (one extra read per NetBox device) instead of
               reusing the in-memory result from Phase 2.
  NetBox data is always fetched in full regardless of --only (except for any
  narrowing --site does; see below); only which Kentik-side phase(s) run is
  affected.

Scoping to a single site:
  --site <name> restricts the run to one NetBox site (exact name match, case
  sensitive): only that site, and devices/container-prefixes scoped to it, are
  fetched from NetBox and synced -- role/tenant/tag labels are still created from
  the full NetBox inventory (cheap and idempotent either way), but assignment only
  ever touches that site's devices, since that's all sync_devices/sync_labels see.
  If the named site doesn't exist in NetBox, the script exits with a clean error
  rather than silently syncing nothing. Composes with --only, e.g. --site DC1
  --only devices syncs just that site's devices (the site itself must already
  exist in Kentik, same as --only devices always requires).

Skipping label assignment:
  --skip-label-assignment creates labels in Kentik as usual but never assigns them to
  any device. This also skips resolving device IDs entirely (no per-device lookup),
  since that work only exists to support assignment. Combine with --only labels to do
  nothing but ensure the label set exists in Kentik.

Network errors:
  A transient network failure (DNS, connection refused/unreachable, timeout) talking
  to either API is retried up to 3 times with a short back-off before giving up. If
  it still fails, the script exits with a single clean error line instead of a raw
  traceback. Since every phase is idempotent (create-or-update, compare-then-set),
  it is always safe to simply re-run the same command after a failure: already
  synced sites/devices/labels are detected as up to date and left alone.

Per-item failures:
  If a single site, device, or label create/update/assignment fails (e.g. a bad
  value rejected by Kentik, or retries exhausted for that one call), the error is
  logged immediately and the run continues with the remaining items rather than
  aborting the whole phase. Every recorded failure is listed in a summary table
  printed at the end of the run, with the reason reduced to Kentik/NetBox's own
  error message (instead of the raw HTTP wrapper) so it's readable at a glance.
  The script exits with a non-zero status if there was at least one failure.
  Since every phase is idempotent, simply re-running the same command retries
  only what failed (everything else is already in sync).

Device update visibility:
  Before updating an existing device, its current state is fetched from Kentik and
  compared field by field against what this run would send; the log line for that
  update names exactly which fields differ and their old -> new values (or says so
  explicitly if nothing actually changed). NMS agent config can't be compared this
  way, so it's only flagged as present rather than diffed.

Site naming:
  --site-name-template controls the Kentik site title, as a str.format template over
  a fixed set of NetBox site fields: {name}, {slug}, {facility}, {region}, {group},
  {tenant} (the .name of each foreign key), and {region_slug}, {group_slug},
  {tenant_slug} (the .slug of the same) -- both variants are always available, so
  which one a template uses is a config choice, not a code change. Default is
  "{name}" (the site's plain NetBox name, unchanged). If any field the template
  actually references is null or empty for a given site, that site falls back to
  its plain NetBox name rather than producing a partial title like "-Riverside".
  A template referencing an unsupported field name is rejected at startup. NetBox
  device records only carry their site's plain name (not region/group/tenant), so
  this mapping is resolved once from the full site list and reused wherever a
  device's site needs to be looked up.

Label sources:
  --label-sources controls which NetBox objects become Kentik labels (Phase 3) and
  whether each one's name or slug is used as the label text, as comma-separated
  "source:field" pairs, e.g. "role:slug,tenant:name". Sources: role, tenant, tag.
  Fields: name, slug. A source left out of the spec entirely is neither created nor
  assigned (e.g. "role:slug,tenant:slug" drops tags without touching the code).
  Default: "role:slug,tenant:slug,tag:slug" (today's behavior). A spec referencing an
  unsupported source or field is rejected at startup. Every label's display text is
  prefixed with its source type, e.g. a device role "core" becomes label
  "role:core", tenant "Acme Corp" becomes "tenant:Acme Corp", tag "prod" becomes
  "tag:prod" -- this keeps labels from different sources from ever colliding and
  makes the source obvious at a glance in Kentik.
"""

import argparse
import json
import logging
import os
import re
import string
import sys
import time
from urllib.parse import quote

import requests
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

NMS_AGENT_TAG = "kentik_primary_agent"

# Only devices in this NetBox status are ever fetched/synced; change this
# one place to widen or narrow it instead of editing every fetch call.
NETBOX_DEVICE_STATUS = "active"

# Fields --site-name-template may reference; see resolve_site_name().
SITE_NAME_TEMPLATE_FIELDS = (
    "name", "slug", "facility",
    "region", "region_slug",
    "group", "group_slug",
    "tenant", "tenant_slug",
)

# NetBox object types --label-sources may draw labels from, and the fields
# each one may be keyed by; see parse_label_sources().
LABEL_SOURCES = ("role", "tenant", "tag")
LABEL_SOURCE_FIELDS = ("name", "slug")
DEFAULT_LABEL_SOURCES_SPEC = "role:slug,tenant:slug,tag:slug"


def parse_label_sources(spec):
    """Parse a --label-sources spec (e.g. "role:slug,tenant:name") into an
    ordered {source: field} dict.

    A source left out of spec entirely is neither created nor assigned as a
    label; this is how e.g. tags can be dropped without touching the code.
    Raises ValueError with a message naming exactly what's wrong, so callers
    can turn that into a clean startup exit instead of a stack trace.
    """
    sources = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        source, sep, field = entry.partition(":")
        source, field = source.strip(), field.strip()
        if not sep:
            raise ValueError(f"invalid --label-sources entry '{entry}': expected 'source:field'")
        if source not in LABEL_SOURCES:
            raise ValueError(
                f"unknown label source '{source}' in --label-sources. Supported: {', '.join(LABEL_SOURCES)}"
            )
        if field not in LABEL_SOURCE_FIELDS:
            raise ValueError(
                f"unknown label field '{field}' for source '{source}' in --label-sources. "
                f"Supported: {', '.join(LABEL_SOURCE_FIELDS)}"
            )
        sources[source] = field
    return sources


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name):
    val = os.environ.get(name)
    return int(val) if val else None


def get_config():
    parser = argparse.ArgumentParser(description="Sync NetBox to Kentik")
    parser.add_argument("--kentik-email", default=os.environ.get("KENTIK_EMAIL"))
    parser.add_argument("--kentik-token", default=os.environ.get("KENTIK_TOKEN"))
    parser.add_argument("--netbox-url", default=os.environ.get("NETBOX_URL"))
    parser.add_argument("--netbox-token", default=os.environ.get("NETBOX_TOKEN"))
    parser.add_argument("--kentik-plan", default=os.environ.get("KENTIK_PLAN_NAME"))
    parser.add_argument("--kentik-region", default=os.environ.get("KENTIK_REGION", "US"),
                        choices=["US", "EU"])
    parser.add_argument("--snmp-community", default=os.environ.get("KENTIK_SNMP_COMMUNITY", ""))
    parser.add_argument("--snmp-credential", default=os.environ.get("KENTIK_SNMP_CRED", "default"))
    parser.add_argument("--sample-rate", type=int,
                        default=int(os.environ.get("KENTIK_SAMPLE_RATE", "1")))
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("DRY_RUN"),
                        help="Log planned changes without applying them to Kentik.")
    parser.add_argument("--limit", type=int, default=_env_int("KENTIK_SYNC_LIMIT"),
                        help="Cap the number of create/update operations performed per "
                             "phase (sites created or updated, devices processed, labels "
                             "created, device label assignments touched). Default: no limit.")
    parser.add_argument("--netbox-insecure-tls", action="store_true",
                        default=_env_bool("NETBOX_INSECURE_TLS"),
                        help="Skip TLS certificate verification for NetBox API requests. "
                             "Only use against trusted internal/lab NetBox instances.")
    parser.add_argument("--only", choices=["sites", "devices", "labels"],
                        default=os.environ.get("KENTIK_SYNC_ONLY") or None,
                        help="Run only one phase instead of the full sites -> devices -> "
                             "labels pipeline. 'labels' resolves each device's Kentik ID "
                             "with an individual lookup instead of reusing Phase 2's result, "
                             "since Phase 2 did not just run. Default: run all three phases.")
    parser.add_argument("--skip-label-assignment", action="store_true",
                        default=_env_bool("KENTIK_SKIP_LABEL_ASSIGNMENT"),
                        help="Create role/tenant/tag labels in Kentik but do not assign them "
                             "to any device. Skips resolving device IDs entirely, so it also "
                             "avoids the extra per-device lookup that --only labels would "
                             "otherwise do.")
    parser.add_argument("--site-name-template",
                        default=os.environ.get("KENTIK_SITE_NAME_TEMPLATE", "{name}"),
                        help="str.format template for the Kentik site title, over NetBox site "
                             f"fields {', '.join('{' + f + '}' for f in SITE_NAME_TEMPLATE_FIELDS)}. "
                             "A site missing a field the template references falls back to its "
                             "plain NetBox name. Default: '{name}' (unchanged).")
    parser.add_argument("--label-sources",
                        default=os.environ.get("KENTIK_LABEL_SOURCES", DEFAULT_LABEL_SOURCES_SPEC),
                        help="Comma-separated 'source:field' pairs selecting which NetBox "
                             f"objects ({', '.join(LABEL_SOURCES)}) become Kentik labels and "
                             f"whether each uses its name or slug as the label text. A source "
                             "left out entirely is neither created nor assigned "
                             f"(e.g. 'role:slug,tenant:slug' drops tags). Default: "
                             f"'{DEFAULT_LABEL_SOURCES_SPEC}' (today's behavior).")
    parser.add_argument("--site", default=os.environ.get("KENTIK_SYNC_SITE"),
                        help="Scope the run to a single NetBox site (exact name match, case "
                             "sensitive) instead of every site: only that site and its devices "
                             "are fetched from NetBox and synced. Composes with --only (e.g. "
                             "--site DC1 --only devices). Default: no scoping, sync everything.")
    cfg = parser.parse_args()

    missing = [name for name, attr in [
        ("KENTIK_EMAIL", "kentik_email"),
        ("KENTIK_TOKEN", "kentik_token"),
        ("NETBOX_URL", "netbox_url"),
        ("NETBOX_TOKEN", "netbox_token"),
    ] if not getattr(cfg, attr)]
    # Only the devices phase assigns a plan to a device, so KENTIK_PLAN_NAME
    # is only required when that phase will actually run.
    if cfg.only in (None, "devices") and not cfg.kentik_plan:
        missing.append("KENTIK_PLAN_NAME")
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")

    if cfg.limit is not None and cfg.limit < 1:
        sys.exit("--limit must be a positive integer")

    referenced_fields = {field for _, field, _, _ in string.Formatter().parse(cfg.site_name_template) if field}
    unknown_fields = referenced_fields - set(SITE_NAME_TEMPLATE_FIELDS)
    if unknown_fields:
        sys.exit(
            f"--site-name-template references unknown field(s): {', '.join(sorted(unknown_fields))}. "
            f"Supported fields: {', '.join(SITE_NAME_TEMPLATE_FIELDS)}"
        )

    try:
        parse_label_sources(cfg.label_sources)
    except ValueError as exc:
        sys.exit(f"--label-sources: {exc}")

    return cfg


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def kentik_request(method, url, headers, payload=None, retries=0):
    """Issue a Kentik API request with rate-limit and network-error back-off and retry."""
    if retries >= 3:
        raise RuntimeError(f"HTTP {method} {url} failed after 3 retries")

    data = json.dumps(payload) if payload is not None else None
    try:
        response = requests.request(method, url, headers=headers, data=data, timeout=30)
    except requests.exceptions.RequestException as exc:
        wait = 5 * (retries + 1)
        log.warning("Network error calling Kentik (%s %s): %s; retrying in %ds", method, url, exc, wait)
        time.sleep(wait)
        return kentik_request(method, url, headers, payload, retries + 1)

    if response.status_code == 429:
        wait = int(response.headers.get("x-ratelimit-reset", 60))
        log.warning("Rate limited; sleeping %ds before retry", wait)
        time.sleep(wait)
        return kentik_request(method, url, headers, payload, retries + 1)

    if response.status_code == 404:
        return None

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"HTTP {method} {url} returned {response.status_code}: {response.text}"
        )

    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining and int(remaining) < 10:
        time.sleep(10)

    return response


def _netbox_get(url, headers, verify, retries=0):
    """GET a NetBox URL with network-error back-off and retry."""
    try:
        return requests.get(url, headers=headers, timeout=30, verify=verify)
    except requests.exceptions.RequestException as exc:
        if retries >= 2:
            raise RuntimeError(f"NetBox GET {url} failed after 3 retries: {exc}") from None
        wait = 5 * (retries + 1)
        log.warning("Network error calling NetBox (GET %s): %s; retrying in %ds", url, exc, wait)
        time.sleep(wait)
        return _netbox_get(url, headers, verify, retries + 1)


def netbox_get_all(url, headers, verify=True):
    """Fetch every page from a NetBox paginated endpoint and return the full list."""
    results = []
    next_url = url
    while next_url:
        resp = _netbox_get(next_url, headers, verify)
        if resp.status_code != 200:
            raise RuntimeError(
                f"NetBox GET {next_url} returned {resp.status_code}: {resp.text}"
            )
        body = resp.json()
        results.extend(body.get("results", []))
        next_url = body.get("next")
    return results


def fetch_scoped_site(nb_base, nb_headers, nb_verify, site_name):
    """Look up exactly one NetBox site by exact (case-sensitive) name, for
    --site scoping. Raises RuntimeError if it doesn't exist, since silently
    syncing nothing would be far more confusing than a clean error naming
    the site that couldn't be found.
    """
    matches = netbox_get_all(
        f"{nb_base}/api/dcim/sites/?name={quote(site_name)}&limit=0", nb_headers, verify=nb_verify
    )
    if not matches:
        raise RuntimeError(f"NetBox site '{site_name}' not found (name match is exact and case sensitive)")
    return matches[0]


# ---------------------------------------------------------------------------
# Kentik API client
# ---------------------------------------------------------------------------

# Versioned Kentik model API paths (relative to self._base). Bump the version
# here when Kentik ships a new one; every request/error message below refers
# to these constants rather than repeating the literal path.
KENTIK_SITES_PATH = "/site/v202509/sites"
KENTIK_LABELS_PATH = "/label/v202210/labels"
KENTIK_DEVICE_PATH = "/device/v202504beta2/device"


class KentikClient:
    def __init__(self, email, token, region="US", dry_run=False):
        if region == "EU":
            self._base = "https://grpc.api.kentik.eu"
            self._v5 = "https://api.kentik.eu/api/v5"
        else:
            self._base = "https://grpc.api.kentik.com"
            self._v5 = "https://api.kentik.com/api/v5"

        self._h = {
            "X-CH-Auth-Email": email,
            "X-CH-Auth-API-Token": token,
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run
        self._dry_run_counter = 0

    def _next_dry_run_id(self):
        """Return a unique placeholder ID for a resource that would be created.

        Always negative, since real Kentik IDs are positive, so callers can
        distinguish a simulated resource from a real one with a simple check.
        """
        self._dry_run_counter -= 1
        return self._dry_run_counter

    # ---- Sites ----

    def get_sites(self):
        """Return {title: site_dict} for all Kentik sites, keyed by title.

        site_dict is the full object returned by Kentik (id, lat, lon,
        postalAddress, type, addressClassification, ...) so callers can
        detect drift and PUT back a complete object without clobbering
        fields they don't otherwise touch.
        """
        resp = kentik_request("GET", f"{self._base}{KENTIK_SITES_PATH}", self._h)
        if resp is None:
            raise RuntimeError(f"Kentik GET {KENTIK_SITES_PATH} returned 404: check API base URL/region")
        return {site["title"]: site for site in resp.json().get("sites", [])}

    def create_site(self, title, lat=0.0, lon=0.0, user_access_networks=None):
        user_access_networks = user_access_networks or []
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create site: %s (lat=%s, lon=%s, userAccessNetworks=%s)",
                title, lat, lon, user_access_networks,
            )
            return self._next_dry_run_id()

        payload = {
            "site": {
                "title": title,
                "lat": lat or 0,
                "lon": lon or 0,
                "type": "SITE_TYPE_OTHER",
                "addressClassification": {
                    "infrastructureNetworks": [],
                    "userAccessNetworks": user_access_networks,
                    "otherNetworks": [],
                },
            }
        }
        resp = kentik_request("POST", f"{self._base}{KENTIK_SITES_PATH}", self._h, payload)
        if resp is None:
            raise RuntimeError(f"Kentik POST {KENTIK_SITES_PATH} returned 404 for site '{title}'")
        return resp.json()["site"]["id"]

    def update_site(self, site_id, site_obj):
        """PUT back a full site object (as fetched from get_sites, with fields
        overridden by the caller) so an update to e.g. lat/lon doesn't clobber
        other Kentik-side fields like postalAddress or siteMarket.
        """
        site_obj = dict(site_obj)
        site_obj["id"] = site_id
        if self.dry_run:
            log.info("[DRY-RUN] Would update site id=%s: %s", site_id,
                      json.dumps(site_obj, indent=2, sort_keys=True))
            return site_id

        resp = kentik_request("PUT", f"{self._base}{KENTIK_SITES_PATH}/{site_id}", self._h, {"site": site_obj})
        if resp is None:
            raise RuntimeError(f"Kentik PUT {KENTIK_SITES_PATH}/{site_id} returned 404")
        return resp.json()["site"]["id"]

    def ensure_site(self, title, lat=0.0, lon=0.0, user_access_networks=None, site_cache=None):
        """Return the Kentik site ID, creating the site if it does not exist."""
        user_access_networks = user_access_networks or []
        if site_cache is None:
            site_cache = {}
        if title not in site_cache:
            log.info("Creating missing site: %s", title)
            new_id = self.create_site(title, lat, lon, user_access_networks)
            site_cache[title] = {
                "id": new_id, "title": title, "lat": lat, "lon": lon,
                "addressClassification": {"userAccessNetworks": user_access_networks},
            }
        return site_cache[title]["id"]

    # ---- Labels ----

    def get_labels(self):
        """Return {name.lower(): id} for all Kentik labels."""
        resp = kentik_request("GET", f"{self._base}{KENTIK_LABELS_PATH}", self._h)
        if resp is None:
            raise RuntimeError(f"Kentik GET {KENTIK_LABELS_PATH} returned 404: check API base URL/region")
        return {label["name"].lower(): label["id"] for label in resp.json().get("labels", [])}

    def create_label(self, name, color):
        if self.dry_run:
            log.info("[DRY-RUN] Would create label: %s (%s)", name, color)
            return self._next_dry_run_id()

        payload = {"label": {"name": name, "color": color}}
        resp = kentik_request("POST", f"{self._base}{KENTIK_LABELS_PATH}", self._h, payload)
        if resp is None:
            raise RuntimeError(f"Kentik POST {KENTIK_LABELS_PATH} returned 404 for label '{name}'")
        return resp.json()["label"]["id"]

    def ensure_label(self, name, color, label_cache):
        """Return the Kentik label ID, creating it if it does not exist."""
        key = name.lower()
        if key not in label_cache:
            log.info("Creating missing label: %s (%s)", name, color)
            try:
                label_cache[key] = self.create_label(name, color)
            except RuntimeError as exc:
                if "already exists" not in str(exc):
                    raise
                log.info("Label %s already exists, refreshing cache", name)
                label_cache.update(self.get_labels())
                if key not in label_cache:
                    raise RuntimeError(f"Label '{name}' exists in Kentik but was not returned by GET /labels") from exc
        return label_cache[key]

    # ---- Plans ----

    def get_plan_id(self, plan_name):
        resp = kentik_request("GET", f"{self._v5}/plans", self._h)
        if resp is None:
            raise RuntimeError("Kentik GET /plans returned 404: check API base URL/region")
        for plan in resp.json().get("plans", []):
            if plan["name"] == plan_name:
                return plan["id"]
        raise RuntimeError(f"Kentik plan '{plan_name}' not found")

    # ---- Devices ----

    def check_device(self, device_name):
        """Return the Kentik device ID if it exists, else None."""
        resp = kentik_request("GET", f"{self._v5}/device/{device_name.lower()}", self._h)
        if resp is None:
            return None
        return resp.json()["device"]["id"]

    def _dry_run_site_note(self, device_obj, site_title):
        """Explain a placeholder siteId in a dry-run log line, if there is one."""
        if site_title is None:
            return ""
        site_id = device_obj.get("siteId")
        if isinstance(site_id, int) and site_id < 0:
            return f" [site '{site_title}' is itself pending creation; placeholder siteId={site_id}]"
        return f" [site '{site_title}']"

    def create_device(self, device_obj, site_title=None):
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would create device%s: %s",
                self._dry_run_site_note(device_obj, site_title),
                json.dumps(device_obj, indent=2, sort_keys=True),
            )
            return self._next_dry_run_id()

        resp = kentik_request(
            "POST", f"{self._base}{KENTIK_DEVICE_PATH}", self._h,
            {"device": device_obj}
        )
        if resp is None:
            raise RuntimeError(f"Kentik POST {KENTIK_DEVICE_PATH} returned 404 for device '{device_obj.get('deviceName')}'")
        return resp.json()["device"]["id"]

    def update_device(self, device_id, device_obj, site_title=None):
        device_obj["id"] = device_id
        if self.dry_run:
            log.info(
                "[DRY-RUN] Would update device id=%s%s: %s",
                device_id,
                self._dry_run_site_note(device_obj, site_title),
                json.dumps(device_obj, indent=2, sort_keys=True),
            )
            return device_id

        resp = kentik_request(
            "PUT", f"{self._base}{KENTIK_DEVICE_PATH}/{device_id}", self._h,
            {"device": device_obj}
        )
        if resp is None:
            raise RuntimeError(f"Kentik PUT {KENTIK_DEVICE_PATH}/{device_id} returned 404")
        return resp.json()["device"]["id"]

    def get_device(self, device_id):
        """Return the full Kentik device object for device_id."""
        resp = kentik_request(
            "GET", f"{self._base}{KENTIK_DEVICE_PATH}/{device_id}", self._h
        )
        if resp is None:
            raise RuntimeError(f"Kentik GET {KENTIK_DEVICE_PATH}/{device_id} returned 404")
        return resp.json()["device"]

    def get_device_label_ids(self, device_id):
        if isinstance(device_id, int) and device_id < 0:
            # Placeholder ID for a device that only exists in this dry run, so it
            # has no real labels to fetch yet.
            return []

        return [label["id"] for label in self.get_device(device_id).get("labels", [])]

    def set_device_labels(self, device_id, label_ids):
        if self.dry_run:
            log.info("[DRY-RUN] Would set labels for device id=%s: %s", device_id, label_ids)
            return

        payload = {
            "id": device_id,
            "labels": [{"id": int(label_id)} for label_id in label_ids],
        }
        kentik_request(
            "PUT", f"{self._base}{KENTIK_DEVICE_PATH}/{device_id}/labels",
            self._h, payload
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_cidr(ip_str):
    """'192.0.2.1/24' -> '192.0.2.1'."""
    return ip_str.split("/")[0] if ip_str and "/" in ip_str else ip_str


def netbox_auth_header(token):
    """Build the Authorization header value for a NetBox API token.

    NetBox 4.6+ issues v2 tokens (format "nbt_<key>.<secret>", sent as a
    Bearer token) and deprecated the legacy v1 tokens (sent as a plain
    Token), removing them in v5.0. Detecting the "nbt_" prefix lets this
    script work unmodified against both older and newer NetBox instances.
    """
    if token.startswith("nbt_"):
        return f"Bearer {token}"
    return f"Token {token}"


def container_prefixes_by_site(netbox_prefixes):
    """Return {site_name: sorted [cidr, ...]} from NetBox prefixes with status
    'container', for prefixes scoped directly to a site. Prefixes scoped to a
    region, site group, or location (or not scoped at all) are not associated
    with a single site and are skipped.
    """
    by_site = {}
    for prefix in netbox_prefixes:
        if prefix.get("scope_type") != "dcim.site":
            continue
        scope = prefix.get("scope") or {}
        site_name = scope.get("name")
        cidr = prefix.get("prefix")
        if not site_name or not cidr:
            continue
        by_site.setdefault(site_name, set()).add(cidr)
    return {site_name: sorted(cidrs) for site_name, cidrs in by_site.items()}


def resolve_site_name(nb_site, template):
    """Render a NetBox site's Kentik site title from `template`, a str.format
    string over SITE_NAME_TEMPLATE_FIELDS. region/group/tenant are NetBox
    foreign keys: {region}/{group}/{tenant} resolve to their .name (e.g.
    "North Carolina"), {region_slug}/{group_slug}/{tenant_slug} to their
    .slug (e.g. "us-nc") -- both variants are always available so which one
    a template uses is purely a config choice. facility/slug/name are
    already plain strings on the site itself.

    If any field the template actually references is null or empty for this
    particular site, falls back to the site's plain NetBox name rather than
    producing a partial title (e.g. "-Riverside" when region is unset).
    """
    context = {
        "name": nb_site.get("name"),
        "slug": nb_site.get("slug"),
        "facility": nb_site.get("facility") or None,
    }
    for field in ("region", "group", "tenant"):
        related = nb_site.get(field)
        context[field] = related.get("name") if related else None
        context[f"{field}_slug"] = related.get("slug") if related else None

    referenced = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    if any(not context.get(field) for field in referenced):
        return nb_site.get("name")
    return template.format(**context)


_HTTP_ERROR_RE = re.compile(r"^HTTP \S+ \S+ returned (\d+): (.*)$", re.DOTALL)


def _clean_failure_reason(reason):
    """Reduce a raw HTTP failure string to just its status code and the API's
    own error message, so it reads on one line instead of being an opaque
    'HTTP POST <url> returned 400: {"code":3,"message":"...","details":[]}'
    blob. Falls back to the original text for anything that doesn't match
    (e.g. the "failed after 3 retries" network-error message).
    """
    match = _HTTP_ERROR_RE.match(reason)
    if not match:
        return reason
    status_code, body = match.groups()
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return f"{status_code}: {body}"
    if isinstance(parsed, dict) and "message" in parsed:
        return f"{status_code}: {parsed['message']}"
    return f"{status_code}: {body}"


def _record_failure(failures, phase, item, exc):
    """Log a single item's failure and record it, instead of letting it abort
    the whole phase. The item is simply left out of this run's result and can
    be retried by re-running the script, since every phase is idempotent.

    The full raw error is logged immediately (for anyone tailing output live),
    while a cleaned-up, one-line version is stored for the end-of-run table.
    """
    reason = str(exc)
    log.error("%s: %s failed: %s", phase, item, reason)
    failures.append({"phase": phase, "item": item, "reason": _clean_failure_reason(reason)})


def format_failures_table(failures):
    """Render failures as a Unicode box-drawing table with columns Phase, Item, Reason."""
    headers = ("Phase", "Item", "Reason")
    max_reason_len = 160

    rows = []
    for failure in failures:
        reason = failure["reason"]
        if len(reason) > max_reason_len:
            reason = reason[: max_reason_len - 1] + "…"
        rows.append((failure["phase"], failure["item"], reason))

    widths = [
        max(len(headers[col]), max((len(row[col]) for row in rows), default=0))
        for col in range(3)
    ]

    def border(left, mid, right):
        return left + mid.join("─" * (width + 2) for width in widths) + right

    def row_line(cells):
        return "│ " + " │ ".join(cell.ljust(widths[col]) for col, cell in enumerate(cells)) + " │"

    lines = [border("┌", "┬", "┐"), row_line(headers), border("├", "┼", "┤")]
    lines.extend(row_line(row) for row in rows)
    lines.append(border("└", "┴", "┘"))
    return "\n".join(lines)


def nms_agent_tag(nb_device):
    """Return agent ID string if the device carries a kentik_primary_agent=<id> tag."""
    prefix = NMS_AGENT_TAG + "="
    tags = nb_device.get("tags", [])
    tag_names = [tag.get("name", "") for tag in tags]
    log.debug("Device %s tags: %s", nb_device.get("name"), tag_names)
    for name in tag_names:
        if name.startswith(prefix):
            return name.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Phase 1 – Sites
# ---------------------------------------------------------------------------

def sync_sites(kentik, netbox_sites, container_prefixes_by_site=None, limit=None, failures=None,
                site_name_template="{name}"):
    """Ensure every NetBox site exists in Kentik with matching lat/lon and
    userAccessNetworks.

    The Kentik site title is rendered from site_name_template (see
    resolve_site_name); by default it's just the NetBox site's plain name,
    unchanged. Missing sites are created; existing sites whose lat/lon or
    userAccessNetworks (the CIDRs of NetBox's container-status prefixes
    scoped to that site) have drifted from NetBox's current values are
    updated. NetBox is treated as the source of truth, but only when NetBox
    actually has a value: a NetBox site with no latitude/longitude set is
    left alone rather than zeroing out a real value that may already be
    configured in Kentik.

    A site whose create/update call fails is recorded in failures (if given)
    and skipped, rather than aborting the sync of every other site.

    Returns {netbox_site_name: kentik_id}, keyed by NetBox's plain site name
    (not the rendered Kentik title) since that's what a NetBox device record
    references when resolving its site.
    """
    log.info("=== Phase 1: Syncing sites (%d NetBox sites) ===", len(netbox_sites))
    site_cache = kentik.get_sites()  # keyed by Kentik title
    container_prefixes_by_site = container_prefixes_by_site or {}
    if failures is None:
        failures = []
    touched = 0
    site_ids_by_netbox_name = {}

    for nb_site in netbox_sites:
        netbox_name = nb_site["name"]
        title = resolve_site_name(nb_site, site_name_template)
        nb_lat = nb_site.get("latitude")
        nb_lon = nb_site.get("longitude")
        nb_lat = float(nb_lat) if nb_lat is not None else None
        nb_lon = float(nb_lon) if nb_lon is not None else None
        nb_networks = container_prefixes_by_site.get(netbox_name, [])

        if title in site_cache:
            log.info("Site already exists: %s", title)
            existing = site_cache[title]
            site_ids_by_netbox_name[netbox_name] = existing["id"]

            changes = {}
            if nb_lat is not None and nb_lon is not None and (
                float(existing.get("lat") or 0) != nb_lat or float(existing.get("lon") or 0) != nb_lon
            ):
                changes["lat/lon"] = f"{existing.get('lat')},{existing.get('lon')} -> {nb_lat},{nb_lon}"

            existing_networks = sorted((existing.get("addressClassification") or {}).get("userAccessNetworks", []) or [])
            if existing_networks != nb_networks:
                changes["userAccessNetworks"] = f"{existing_networks} -> {nb_networks}"

            if not changes:
                continue

            if limit is not None and touched >= limit:
                log.info("Site limit (%d) reached; skipping update for: %s", limit, title)
                continue

            log.info(
                "Site %s: %s; updating",
                title, "; ".join(f"{field} drifted ({diff})" for field, diff in changes.items()),
            )
            updated_site = dict(existing)
            if "lat/lon" in changes:
                updated_site["lat"], updated_site["lon"] = nb_lat, nb_lon
            if "userAccessNetworks" in changes:
                address_classification = dict(existing.get("addressClassification") or {})
                address_classification["userAccessNetworks"] = nb_networks
                updated_site["addressClassification"] = address_classification

            try:
                kentik.update_site(existing["id"], updated_site)
            except RuntimeError as exc:
                _record_failure(failures, "sites", title, exc)
                continue
            existing.update(updated_site)
            touched += 1
            continue

        if limit is not None and touched >= limit:
            log.info("Site limit (%d) reached; skipping site: %s", limit, title)
            continue

        try:
            new_id = kentik.ensure_site(
                title=title, lat=nb_lat or 0.0, lon=nb_lon or 0.0,
                user_access_networks=nb_networks, site_cache=site_cache,
            )
        except RuntimeError as exc:
            _record_failure(failures, "sites", title, exc)
            continue
        touched += 1
        site_ids_by_netbox_name[netbox_name] = new_id

    log.info("Phase 1 complete. %d sites in Kentik.", len(site_cache))
    return site_ids_by_netbox_name


# ---------------------------------------------------------------------------
# Phase 2 – Devices
# ---------------------------------------------------------------------------

# Fields this script writes that come back under the same key name when a
# device is read back via KentikClient.get_device.
_DEVICE_DIFF_FIELDS = (
    "deviceDescription", "deviceSubtype", "deviceSampleRate", "deviceBgpType",
    "minimizeSnmp", "sendingIps", "deviceSnmpIp", "deviceSnmpCommunity",
)


def _device_field_changes(existing_device, desired_device):
    """Compare a device object read from Kentik against the payload this
    script is about to send, and return {field: "old -> new"} for every
    field that actually differs. siteId/planId are compared against the
    read side's nested site.id/plan.id, since Kentik returns those as
    objects on read but expects flat IDs on write. NMS config isn't
    comparable this way (it isn't echoed back in a matching shape), so it's
    only flagged as present rather than diffed.
    """
    changes = {}
    for field in _DEVICE_DIFF_FIELDS:
        if field not in desired_device:
            continue
        old, new = existing_device.get(field), desired_device[field]
        if str(old) != str(new):
            changes[field] = f"{old!r} -> {new!r}"

    old_site_id = str((existing_device.get("site") or {}).get("id"))
    new_site_id = str(desired_device.get("siteId"))
    if old_site_id != new_site_id:
        changes["siteId"] = f"{old_site_id} -> {new_site_id}"

    old_plan_id = str((existing_device.get("plan") or {}).get("id"))
    new_plan_id = str(desired_device.get("planId"))
    if old_plan_id != new_plan_id:
        changes["planId"] = f"{old_plan_id} -> {new_plan_id}"

    if "nms" in desired_device:
        changes["nms"] = "NMS agent config included (not diffed)"

    return changes


def sync_devices(kentik, netbox_devices, site_cache, plan_id, cfg, limit=None, failures=None):
    """Create or update every NetBox device in Kentik. Returns {name: kentik_device_id}.

    A device whose create/update call fails is recorded in failures (if
    given) and skipped, rather than aborting the sync of every other device.
    """
    log.info("=== Phase 2: Syncing devices (%d NetBox devices) ===", len(netbox_devices))
    device_ids = {}
    processed = 0
    if failures is None:
        failures = []

    for nb_device in netbox_devices:
        if limit is not None and processed >= limit:
            log.info("Device limit (%d) reached; stopping further device processing", limit)
            break

        device_name = nb_device.get("name")
        if not device_name:
            log.warning("Device id=%s has no name in NetBox (e.g. a virtual chassis member), skipping", nb_device.get("id"))
            continue
        name = device_name.lower()

        # Resolve site
        nb_site = nb_device.get("site") or {}
        site_name = nb_site.get("name")
        if not site_name or site_name not in site_cache:
            log.warning("Device %s: site '%s' not found in Kentik, skipping", name, site_name)
            continue
        site_id = site_cache[site_name]

        # Primary IP
        primary_ip4 = nb_device.get("primary_ip4")
        ip_address = strip_cidr(primary_ip4.get("address")) if primary_ip4 else None

        # NMS agent
        agent_id = nms_agent_tag(nb_device)

        device_obj = {
            "deviceName": name,
            "deviceDescription": nb_device.get("description") or "Synced from NetBox",
            "deviceSubtype": "router",
            "deviceSampleRate": cfg.sample_rate,
            "planId": int(plan_id),
            "siteId": int(site_id),
            "deviceBgpType": "none",
            "minimizeSnmp": False,
        }

        if ip_address:
            device_obj["sendingIps"] = [ip_address]
            device_obj["deviceSnmpIp"] = ip_address

        if cfg.snmp_community:
            device_obj["deviceSnmpCommunity"] = cfg.snmp_community

        if agent_id and ip_address:
            log.info("Device %s: configuring NMS with agent=%s ip=%s", name, agent_id, ip_address)
            device_obj["nms"] = {
                "agentId": agent_id,
                "ipAddress": ip_address,
                "snmp": {"credentialName": cfg.snmp_credential},
            }
        elif agent_id and not ip_address:
            log.warning("Device %s: NMS agent tag found (id=%s) but no primary IP, skipping NMS", name, agent_id)

        try:
            existing_id = kentik.check_device(name)
            if existing_id:
                existing_device = kentik.get_device(existing_id)
                changes = _device_field_changes(existing_device, device_obj)
                if changes:
                    log.info(
                        "Updating device %s (id=%s): %s",
                        name, existing_id,
                        "; ".join(f"{field} {diff}" for field, diff in changes.items()),
                    )
                else:
                    log.info("Updating device: %s (id=%s) (no field changes detected)", name, existing_id)
                kentik.update_device(existing_id, device_obj, site_title=site_name)
                device_ids[name] = existing_id
            else:
                log.info("Creating device: %s", name)
                device_ids[name] = kentik.create_device(device_obj, site_title=site_name)
        except RuntimeError as exc:
            _record_failure(failures, "devices", name, exc)
            continue

        processed += 1

    log.info("Phase 2 complete. %d devices synced.", len(device_ids))
    return device_ids


# ---------------------------------------------------------------------------
# Phase 3 – Labels
# ---------------------------------------------------------------------------

def lookup_device_ids(kentik, netbox_devices, failures=None):
    """Resolve {name: kentik_device_id} via an individual read per NetBox device.

    Used to run Phase 3 with --only labels, where Phase 2 did not just run so
    there's no in-memory device_ids result to reuse. A device whose lookup
    fails (as opposed to simply not existing yet) is recorded in failures
    (if given) and skipped.
    """
    if failures is None:
        failures = []
    device_ids = {}
    for nb_device in netbox_devices:
        device_name = nb_device.get("name")
        if not device_name:
            continue
        name = device_name.lower()
        try:
            existing_id = kentik.check_device(name)
        except RuntimeError as exc:
            _record_failure(failures, "labels: assign", name, exc)
            continue
        if existing_id:
            device_ids[name] = existing_id
        else:
            log.warning("Device %s not found in Kentik; skipping label assignment", name)
    return device_ids


def _label_key(value):
    """Normalize a label's display text into label_cache's lookup key, the
    same way KentikClient.ensure_label does (name.lower()). Returns None for
    a missing/empty value so callers can skip it with a plain 'in' check.
    """
    return value.lower() if value else None


def _prefixed_label_value(source, value):
    """Build a label's display text with its source-type prefix, e.g.
    "role:core" or "tenant:Acme Corp" or "tag:prod", so labels from
    different NetBox object types can't collide and it's clear in Kentik
    which kind of label one is. Returns None if value is missing/empty, so
    callers treat that the same as "no label for this dimension" instead of
    creating one literally named e.g. "role:None".
    """
    return f"{source}:{value}" if value else None


def sync_labels(kentik, netbox_devices, netbox_roles, netbox_tenants, netbox_tags, device_ids,
                 limit=None, skip_assignment=False, failures=None, label_sources=None):
    """Create labels from NetBox metadata and, unless skip_assignment, assign them to devices.

    label_sources is an ordered {source: field} dict from parse_label_sources
    (default DEFAULT_LABEL_SOURCES_SPEC, i.e. role/tenant/tag all keyed by
    slug -- today's behavior). A source missing from the dict is neither
    created nor assigned; the field ("name" or "slug") controls which NetBox
    attribute becomes the label's display text, for both creation and the
    device-assignment lookup.

    device_ids may be None, meaning Phase 2 did not just run (e.g. --only
    labels); in that case each device's Kentik ID is resolved individually
    right before assignment, after labels have already been created. That
    resolution (one Kentik read per NetBox device) is skipped entirely when
    skip_assignment is set, since it would only be needed for assignment.

    A label create or device label assignment that fails is recorded in
    failures (if given) and skipped, rather than aborting the whole phase.
    """
    log.info("=== Phase 3: Syncing labels ===")
    label_cache = kentik.get_labels()
    created = 0
    if failures is None:
        failures = []
    if label_sources is None:
        label_sources = parse_label_sources(DEFAULT_LABEL_SOURCES_SPEC)

    def ensure_within_budget(name, color):
        nonlocal created
        key = name.lower()
        if key not in label_cache and limit is not None and created >= limit:
            log.info("Label limit (%d) reached; skipping label: %s", limit, name)
            return
        was_new = key not in label_cache
        try:
            kentik.ensure_label(name, color, label_cache)
        except RuntimeError as exc:
            _record_failure(failures, "labels: create", name, exc)
            return
        if was_new:
            created += 1

    # --- Create labels ---
    # Every label's display text is prefixed with its source type
    # ("role:core", "tenant:Acme Corp", "tag:prod") so labels from different
    # NetBox object types can never collide and it's obvious in Kentik which
    # kind of label one is.
    if "role" in label_sources:
        field = label_sources["role"]
        log.info("Ensuring role labels (using %s)...", field)
        for role in netbox_roles:
            value = _prefixed_label_value("role", role.get(field))
            if not value:
                continue
            color = f"#{role.get('color', '808080')}"
            ensure_within_budget(value, color)
    else:
        log.info("Skipping role labels (not in --label-sources)")

    if "tenant" in label_sources:
        field = label_sources["tenant"]
        log.info("Ensuring tenant labels (using %s)...", field)
        for tenant in netbox_tenants:
            value = _prefixed_label_value("tenant", tenant.get(field))
            if not value:
                continue
            ensure_within_budget(value, "#00ff00")
    else:
        log.info("Skipping tenant labels (not in --label-sources)")

    if "tag" in label_sources:
        field = label_sources["tag"]
        log.info("Ensuring tag labels (using %s)...", field)
        for tag in netbox_tags:
            if tag.get("name", "").startswith(NMS_AGENT_TAG):
                continue  # internal control tag, not a metadata label
            value = _prefixed_label_value("tag", tag.get(field))
            if not value:
                continue
            color = f"#{tag.get('color', '808080')}"
            ensure_within_budget(value, color)
    else:
        log.info("Skipping tag labels (not in --label-sources)")

    # --- Assign labels to devices ---
    if skip_assignment:
        log.info("Skipping device label assignment (--skip-label-assignment)")
        log.info("Phase 3 complete.")
        return

    if device_ids is None:
        log.info("Resolving device IDs for label assignment...")
        device_ids = lookup_device_ids(kentik, netbox_devices, failures=failures)

    log.info("Assigning labels to devices...")
    assigned = 0
    for nb_device in netbox_devices:
        if limit is not None and assigned >= limit:
            log.info("Device label-assignment limit (%d) reached; stopping further assignments", limit)
            break

        device_name = nb_device.get("name")
        if not device_name:
            continue  # unnamed devices (e.g. virtual chassis members) were never synced in Phase 2
        name = device_name.lower()
        device_id = device_ids.get(name)
        if not device_id:
            continue

        desired = []

        if "role" in label_sources and nb_device.get("role"):
            value = _prefixed_label_value("role", nb_device["role"].get(label_sources["role"]))
            key = _label_key(value)
            if key in label_cache:
                desired.append(label_cache[key])

        if "tenant" in label_sources and nb_device.get("tenant"):
            value = _prefixed_label_value("tenant", nb_device["tenant"].get(label_sources["tenant"]))
            key = _label_key(value)
            if key in label_cache:
                desired.append(label_cache[key])

        if "tag" in label_sources:
            field = label_sources["tag"]
            for tag in nb_device.get("tags", []):
                if tag.get("name", "").startswith(NMS_AGENT_TAG):
                    continue
                key = _label_key(_prefixed_label_value("tag", tag.get(field)))
                if key in label_cache:
                    desired.append(label_cache[key])

        if not desired:
            continue

        try:
            existing = kentik.get_device_label_ids(device_id)
            merged = list(set(existing + desired))
            # Compare via string coercion: in dry-run mode a device's desired
            # labels can be a mix of real string IDs (already in Kentik) and
            # negative-int placeholder IDs (would-be-created this run), and
            # sorted() can't compare int to str directly.
            if sorted(str(x) for x in merged) != sorted(str(x) for x in existing):
                log.info("Setting labels for %s: %s", name, merged)
                kentik.set_device_labels(device_id, merged)
        except RuntimeError as exc:
            _record_failure(failures, "labels: assign", name, exc)
            continue
        assigned += 1

    log.info("Phase 3 complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    cfg = get_config()

    if cfg.dry_run:
        log.info("DRY-RUN mode: reads happen normally, but no changes will be sent to Kentik")
    if cfg.limit is not None:
        log.info("Limit active: capping mutating operations to %d per phase", cfg.limit)

    if cfg.netbox_insecure_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log.warning(
            "TLS certificate verification is disabled for NetBox requests "
            "(--netbox-insecure-tls). Only use this against trusted internal/lab instances."
        )

    nb_headers = {
        "Authorization": netbox_auth_header(cfg.netbox_token),
        "Content-Type": "application/json",
    }
    nb_base = cfg.netbox_url.rstrip("/")
    nb_verify = not cfg.netbox_insecure_tls

    log.info("Fetching data from NetBox at %s ...", nb_base)
    if cfg.site:
        log.info("Scoping sync to NetBox site: '%s'", cfg.site)
        scoped_site = fetch_scoped_site(nb_base, nb_headers, nb_verify, cfg.site)
        netbox_sites = [scoped_site]
        netbox_devices = netbox_get_all(
            f"{nb_base}/api/dcim/devices/?status={NETBOX_DEVICE_STATUS}&site_id={scoped_site['id']}&limit=0",
            nb_headers, verify=nb_verify,
        )
        netbox_container_prefixes = netbox_get_all(
            f"{nb_base}/api/ipam/prefixes/?status=container&site_id={scoped_site['id']}&limit=0",
            nb_headers, verify=nb_verify,
        )
    else:
        netbox_sites = netbox_get_all(f"{nb_base}/api/dcim/sites/?limit=0", nb_headers, verify=nb_verify)
        netbox_devices = netbox_get_all(
            f"{nb_base}/api/dcim/devices/?status={NETBOX_DEVICE_STATUS}&limit=0", nb_headers, verify=nb_verify
        )
        netbox_container_prefixes = netbox_get_all(
            f"{nb_base}/api/ipam/prefixes/?status=container&limit=0", nb_headers, verify=nb_verify
        )
    # Roles/tenants/tags are shared taxonomies, not scoped to a site, so
    # these are always fetched in full: label creation is cheap and
    # idempotent regardless of --site, and assignment is already limited to
    # netbox_devices above.
    netbox_roles = netbox_get_all(f"{nb_base}/api/dcim/device-roles/?limit=0", nb_headers, verify=nb_verify)
    netbox_tenants = netbox_get_all(f"{nb_base}/api/tenancy/tenants/?limit=0", nb_headers, verify=nb_verify)
    netbox_tags = netbox_get_all(f"{nb_base}/api/extras/tags/?limit=0", nb_headers, verify=nb_verify)
    log.info(
        "NetBox data: %d sites, %d devices, %d roles, %d tenants, %d tags, %d container prefixes",
        len(netbox_sites), len(netbox_devices), len(netbox_roles),
        len(netbox_tenants), len(netbox_tags), len(netbox_container_prefixes),
    )
    site_networks = container_prefixes_by_site(netbox_container_prefixes)

    kentik = KentikClient(cfg.kentik_email, cfg.kentik_token, cfg.kentik_region, dry_run=cfg.dry_run)

    if cfg.only:
        log.info("--only %s: running just that phase", cfg.only)

    run_sites = cfg.only in (None, "sites")
    run_devices = cfg.only in (None, "devices")
    run_labels = cfg.only in (None, "labels")

    plan_id = None
    if run_devices:
        plan_id = kentik.get_plan_id(cfg.kentik_plan)
        log.info("Kentik plan: '%s' (id=%s)", cfg.kentik_plan, plan_id)

    # Shared across phases: a per-item failure (e.g. one bad device) is logged
    # and recorded here instead of aborting the rest of the run, so a single
    # bad record doesn't block everything else from syncing.
    failures = []

    if run_sites:
        site_cache = sync_sites(kentik, netbox_sites, container_prefixes_by_site=site_networks,
                                 limit=cfg.limit, failures=failures,
                                 site_name_template=cfg.site_name_template)
    elif run_devices:
        # Devices still need to resolve existing sites, just without Phase 1's
        # create/update logic running. NetBox device records only carry their
        # site's plain name, so bridge Kentik's title-keyed sites back to
        # NetBox names via the same template Phase 1 would have used.
        kentik_sites_by_title = kentik.get_sites()
        site_cache = {}
        for nb_site in netbox_sites:
            title = resolve_site_name(nb_site, cfg.site_name_template)
            if title in kentik_sites_by_title:
                site_cache[nb_site["name"]] = kentik_sites_by_title[title]["id"]
    else:
        site_cache = {}

    if run_devices:
        device_ids = sync_devices(kentik, netbox_devices, site_cache, plan_id, cfg,
                                   limit=cfg.limit, failures=failures)
    else:
        # None (rather than {}) signals sync_labels to resolve device IDs
        # itself, after labels are created, if it ends up needing them.
        device_ids = None

    if run_labels:
        sync_labels(kentik, netbox_devices, netbox_roles, netbox_tenants, netbox_tags, device_ids,
                    limit=cfg.limit, skip_assignment=cfg.skip_label_assignment, failures=failures,
                    label_sources=parse_label_sources(cfg.label_sources))

    if failures:
        log.error("Sync completed with %d failure(s):", len(failures))
        print()
        print(format_failures_table(failures))
        print()
        sys.exit(1)

    log.info("Sync complete.")


def _run_cli():
    """Run main(), converting a RuntimeError into a clean one-line exit instead
    of a raw traceback. Unexpected exceptions still propagate in full, since
    those indicate a bug rather than an expected failure mode (network/API error).
    """
    try:
        main()
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    _run_cli()
