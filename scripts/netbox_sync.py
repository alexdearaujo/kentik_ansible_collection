#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Sync NetBox devices, sites, and labels to Kentik.

Execution order:
  1. Sites  – ensure every NetBox site exists in Kentik (create if missing).
  2. Devices – create or update every NetBox device in Kentik.
  3. Labels  – create role/tenant/tag labels, then assign them to devices.

Configuration (env vars or CLI flags):
  KENTIK_EMAIL          Kentik API email                --kentik-email
  KENTIK_TOKEN          Kentik API token                --kentik-token
  NETBOX_URL            NetBox base URL                 --netbox-url
  NETBOX_TOKEN          NetBox API token (v1 or v2)     --netbox-token
  KENTIK_PLAN_NAME      Kentik plan name                --kentik-plan
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
  NetBox data is always fetched in full regardless of --only; only which
  Kentik-side phase(s) run is affected.

Skipping label assignment:
  --skip-label-assignment creates role/tenant/tag labels in Kentik as usual but never
  assigns them to any device. This also skips resolving device IDs entirely (no
  per-device lookup), since that work only exists to support assignment. Combine with
  --only labels to do nothing but ensure the label set exists in Kentik.

Network errors:
  A transient network failure (DNS, connection refused/unreachable, timeout) talking
  to either API is retried up to 3 times with a short back-off before giving up. If
  it still fails, the script exits with a single clean error line instead of a raw
  traceback. Since every phase is idempotent (create-or-update, compare-then-set),
  it is always safe to simply re-run the same command after a failure: already
  synced sites/devices/labels are detected as up to date and left alone.
"""

import argparse
import json
import logging
import os
import sys
import time

import requests
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

NMS_AGENT_TAG = "kentik_primary_agent"


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
    cfg = parser.parse_args()

    missing = [name for name, attr in [
        ("KENTIK_EMAIL", "kentik_email"),
        ("KENTIK_TOKEN", "kentik_token"),
        ("NETBOX_URL", "netbox_url"),
        ("NETBOX_TOKEN", "netbox_token"),
        ("KENTIK_PLAN_NAME", "kentik_plan"),
    ] if not getattr(cfg, attr)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")

    if cfg.limit is not None and cfg.limit < 1:
        sys.exit("--limit must be a positive integer")

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


# ---------------------------------------------------------------------------
# Kentik API client
# ---------------------------------------------------------------------------

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
        resp = kentik_request("GET", f"{self._base}/site/v202211/sites", self._h)
        if resp is None:
            raise RuntimeError("Kentik GET /site/v202211/sites returned 404: check API base URL/region")
        return {s["title"]: s for s in resp.json().get("sites", [])}

    def create_site(self, title, lat=0.0, lon=0.0):
        if self.dry_run:
            log.info("[DRY-RUN] Would create site: %s (lat=%s, lon=%s)", title, lat, lon)
            return self._next_dry_run_id()

        payload = {
            "site": {
                "title": title,
                "lat": lat or 0,
                "lon": lon or 0,
                "type": "SITE_TYPE_OTHER",
                "addressClassification": {
                    "infrastructureNetworks": [],
                    "userAccessNetworks": [],
                    "otherNetworks": [],
                },
            }
        }
        resp = kentik_request("POST", f"{self._base}/site/v202211/sites", self._h, payload)
        if resp is None:
            raise RuntimeError(f"Kentik POST /site/v202211/sites returned 404 for site '{title}'")
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

        resp = kentik_request("PUT", f"{self._base}/site/v202211/sites/{site_id}", self._h, {"site": site_obj})
        if resp is None:
            raise RuntimeError(f"Kentik PUT /site/v202211/sites/{site_id} returned 404")
        return resp.json()["site"]["id"]

    def ensure_site(self, title, lat=0.0, lon=0.0, site_cache=None):
        """Return the Kentik site ID, creating the site if it does not exist."""
        if site_cache is None:
            site_cache = {}
        if title not in site_cache:
            log.info("Creating missing site: %s", title)
            new_id = self.create_site(title, lat, lon)
            site_cache[title] = {"id": new_id, "title": title, "lat": lat, "lon": lon}
        return site_cache[title]["id"]

    # ---- Labels ----

    def get_labels(self):
        """Return {name.lower(): id} for all Kentik labels."""
        resp = kentik_request("GET", f"{self._base}/label/v202210/labels", self._h)
        if resp is None:
            raise RuntimeError("Kentik GET /label/v202210/labels returned 404: check API base URL/region")
        return {l["name"].lower(): l["id"] for l in resp.json().get("labels", [])}

    def create_label(self, name, color):
        if self.dry_run:
            log.info("[DRY-RUN] Would create label: %s (%s)", name, color)
            return self._next_dry_run_id()

        payload = {"label": {"name": name, "color": color}}
        resp = kentik_request("POST", f"{self._base}/label/v202210/labels", self._h, payload)
        if resp is None:
            raise RuntimeError(f"Kentik POST /label/v202210/labels returned 404 for label '{name}'")
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
            "POST", f"{self._base}/device/v202504beta2/device", self._h,
            {"device": device_obj}
        )
        if resp is None:
            raise RuntimeError(f"Kentik POST /device/v202504beta2/device returned 404 for device '{device_obj.get('deviceName')}'")
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
            "PUT", f"{self._base}/device/v202504beta2/device/{device_id}", self._h,
            {"device": device_obj}
        )
        if resp is None:
            raise RuntimeError(f"Kentik PUT /device/v202504beta2/device/{device_id} returned 404")
        return resp.json()["device"]["id"]

    def get_device_label_ids(self, device_id):
        if isinstance(device_id, int) and device_id < 0:
            # Placeholder ID for a device that only exists in this dry run, so it
            # has no real labels to fetch yet.
            return []

        resp = kentik_request(
            "GET", f"{self._base}/device/v202504beta2/device/{device_id}", self._h
        )
        if resp is None:
            raise RuntimeError(f"Kentik GET /device/v202504beta2/device/{device_id} returned 404")
        return [lbl["id"] for lbl in resp.json()["device"].get("labels", [])]

    def set_device_labels(self, device_id, label_ids):
        if self.dry_run:
            log.info("[DRY-RUN] Would set labels for device id=%s: %s", device_id, label_ids)
            return

        payload = {
            "id": device_id,
            "labels": [{"id": int(lid)} for lid in label_ids],
        }
        kentik_request(
            "PUT", f"{self._base}/device/v202504beta2/device/{device_id}/labels",
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


def nms_agent_tag(nb_device):
    """Return agent ID string if the device carries a kentik_primary_agent=<id> tag."""
    prefix = NMS_AGENT_TAG + "="
    tags = nb_device.get("tags", [])
    tag_names = [t.get("name", "") for t in tags]
    log.debug("Device %s tags: %s", nb_device.get("name"), tag_names)
    for name in tag_names:
        if name.startswith(prefix):
            return name.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Phase 1 – Sites
# ---------------------------------------------------------------------------

def sync_sites(kentik, netbox_sites, limit=None):
    """Ensure every NetBox site exists in Kentik with matching lat/lon.

    Missing sites are created; existing sites whose lat/lon has drifted from
    NetBox's current values are updated. NetBox is treated as the source of
    truth for coordinates, but only when NetBox actually has a value: a
    NetBox site with no latitude/longitude set is left alone rather than
    zeroing out a real value that may already be configured in Kentik.

    Returns {name: kentik_id}.
    """
    log.info("=== Phase 1: Syncing sites (%d NetBox sites) ===", len(netbox_sites))
    site_cache = kentik.get_sites()
    touched = 0

    for nb_site in netbox_sites:
        name = nb_site["name"]
        nb_lat = nb_site.get("latitude")
        nb_lon = nb_site.get("longitude")
        nb_lat = float(nb_lat) if nb_lat is not None else None
        nb_lon = float(nb_lon) if nb_lon is not None else None

        if name in site_cache:
            log.info("Site already exists: %s", name)
            existing = site_cache[name]
            if nb_lat is None or nb_lon is None:
                continue
            if float(existing.get("lat") or 0) == nb_lat and float(existing.get("lon") or 0) == nb_lon:
                continue

            if limit is not None and touched >= limit:
                log.info("Site limit (%d) reached; skipping lat/lon update for: %s", limit, name)
                continue

            log.info(
                "Site %s: lat/lon drifted (Kentik %s,%s -> NetBox %s,%s); updating",
                name, existing.get("lat"), existing.get("lon"), nb_lat, nb_lon,
            )
            updated_site = dict(existing, lat=nb_lat, lon=nb_lon)
            kentik.update_site(existing["id"], updated_site)
            existing["lat"], existing["lon"] = nb_lat, nb_lon
            touched += 1
            continue

        if limit is not None and touched >= limit:
            log.info("Site limit (%d) reached; skipping site: %s", limit, name)
            continue

        kentik.ensure_site(title=name, lat=nb_lat or 0.0, lon=nb_lon or 0.0, site_cache=site_cache)
        touched += 1

    log.info("Phase 1 complete. %d sites in Kentik.", len(site_cache))
    return {title: meta["id"] for title, meta in site_cache.items()}


# ---------------------------------------------------------------------------
# Phase 2 – Devices
# ---------------------------------------------------------------------------

def sync_devices(kentik, netbox_devices, site_cache, plan_id, cfg, limit=None):
    """Create or update every NetBox device in Kentik. Returns {name: kentik_device_id}."""
    log.info("=== Phase 2: Syncing devices (%d NetBox devices) ===", len(netbox_devices))
    device_ids = {}
    processed = 0

    for nb in netbox_devices:
        if limit is not None and processed >= limit:
            log.info("Device limit (%d) reached; stopping further device processing", limit)
            break

        device_name = nb.get("name")
        if not device_name:
            log.warning("Device id=%s has no name in NetBox (e.g. a virtual chassis member), skipping", nb.get("id"))
            continue
        name = device_name.lower()

        # Resolve site
        nb_site = nb.get("site") or {}
        site_name = nb_site.get("name")
        if not site_name or site_name not in site_cache:
            log.warning("Device %s: site '%s' not found in Kentik, skipping", name, site_name)
            continue
        site_id = site_cache[site_name]

        # Primary IP
        primary_ip4 = nb.get("primary_ip4")
        ip_address = strip_cidr(primary_ip4.get("address")) if primary_ip4 else None

        # NMS agent
        agent_id = nms_agent_tag(nb)

        device_obj = {
            "deviceName": name,
            "deviceDescription": nb.get("description") or "Synced from NetBox",
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

        existing_id = kentik.check_device(name)
        if existing_id:
            log.info("Updating device: %s (id=%s)", name, existing_id)
            kentik.update_device(existing_id, device_obj, site_title=site_name)
            device_ids[name] = existing_id
        else:
            log.info("Creating device: %s", name)
            device_ids[name] = kentik.create_device(device_obj, site_title=site_name)

        processed += 1

    log.info("Phase 2 complete. %d devices synced.", len(device_ids))
    return device_ids


# ---------------------------------------------------------------------------
# Phase 3 – Labels
# ---------------------------------------------------------------------------

def lookup_device_ids(kentik, netbox_devices):
    """Resolve {name: kentik_device_id} via an individual read per NetBox device.

    Used to run Phase 3 with --only labels, where Phase 2 did not just run so
    there's no in-memory device_ids result to reuse.
    """
    device_ids = {}
    for nb in netbox_devices:
        device_name = nb.get("name")
        if not device_name:
            continue
        name = device_name.lower()
        existing_id = kentik.check_device(name)
        if existing_id:
            device_ids[name] = existing_id
        else:
            log.warning("Device %s not found in Kentik; skipping label assignment", name)
    return device_ids


def sync_labels(kentik, netbox_devices, netbox_roles, netbox_tenants, netbox_tags, device_ids,
                 limit=None, skip_assignment=False):
    """Create labels from NetBox metadata and, unless skip_assignment, assign them to devices.

    device_ids may be None, meaning Phase 2 did not just run (e.g. --only
    labels); in that case each device's Kentik ID is resolved individually
    right before assignment, after labels have already been created. That
    resolution (one Kentik read per NetBox device) is skipped entirely when
    skip_assignment is set, since it would only be needed for assignment.
    """
    log.info("=== Phase 3: Syncing labels ===")
    label_cache = kentik.get_labels()
    created = 0

    def ensure_within_budget(name, color):
        nonlocal created
        key = name.lower()
        if key not in label_cache and limit is not None and created >= limit:
            log.info("Label limit (%d) reached; skipping label: %s", limit, name)
            return
        was_new = key not in label_cache
        kentik.ensure_label(name, color, label_cache)
        if was_new:
            created += 1

    # --- Create labels ---
    log.info("Ensuring role labels...")
    for role in netbox_roles:
        color = f"#{role.get('color', '808080')}"
        ensure_within_budget(role["slug"], color)

    log.info("Ensuring tenant labels...")
    for tenant in netbox_tenants:
        ensure_within_budget(tenant["slug"], "#00ff00")

    log.info("Ensuring tag labels...")
    for tag in netbox_tags:
        if tag.get("name", "").startswith(NMS_AGENT_TAG):
            continue  # internal control tag, not a metadata label
        color = f"#{tag.get('color', '808080')}"
        ensure_within_budget(tag["slug"], color)

    # --- Assign labels to devices ---
    if skip_assignment:
        log.info("Skipping device label assignment (--skip-label-assignment)")
        log.info("Phase 3 complete.")
        return

    if device_ids is None:
        log.info("Resolving device IDs for label assignment...")
        device_ids = lookup_device_ids(kentik, netbox_devices)

    log.info("Assigning labels to devices...")
    assigned = 0
    for nb in netbox_devices:
        if limit is not None and assigned >= limit:
            log.info("Device label-assignment limit (%d) reached; stopping further assignments", limit)
            break

        device_name = nb.get("name")
        if not device_name:
            continue  # unnamed devices (e.g. virtual chassis members) were never synced in Phase 2
        name = device_name.lower()
        device_id = device_ids.get(name)
        if not device_id:
            continue

        desired = []

        if nb.get("role"):
            slug = nb["role"]["slug"]
            if slug in label_cache:
                desired.append(label_cache[slug])

        if nb.get("tenant"):
            slug = nb["tenant"]["slug"]
            if slug in label_cache:
                desired.append(label_cache[slug])

        for tag in nb.get("tags", []):
            if tag.get("name", "").startswith(NMS_AGENT_TAG):
                continue
            slug = tag.get("slug", "")
            if slug in label_cache:
                desired.append(label_cache[slug])

        if not desired:
            continue

        assigned += 1
        existing = kentik.get_device_label_ids(device_id)
        merged = list(set(existing + desired))
        if sorted(merged) != sorted(existing):
            log.info("Setting labels for %s: %s", name, merged)
            kentik.set_device_labels(device_id, merged)

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
    netbox_sites = netbox_get_all(f"{nb_base}/api/dcim/sites/?limit=0", nb_headers, verify=nb_verify)
    netbox_devices = netbox_get_all(f"{nb_base}/api/dcim/devices/?limit=0", nb_headers, verify=nb_verify)
    netbox_roles = netbox_get_all(f"{nb_base}/api/dcim/device-roles/?limit=0", nb_headers, verify=nb_verify)
    netbox_tenants = netbox_get_all(f"{nb_base}/api/tenancy/tenants/?limit=0", nb_headers, verify=nb_verify)
    netbox_tags = netbox_get_all(f"{nb_base}/api/extras/tags/?limit=0", nb_headers, verify=nb_verify)
    log.info(
        "NetBox data: %d sites, %d devices, %d roles, %d tenants, %d tags",
        len(netbox_sites), len(netbox_devices), len(netbox_roles),
        len(netbox_tenants), len(netbox_tags),
    )

    kentik = KentikClient(cfg.kentik_email, cfg.kentik_token, cfg.kentik_region, dry_run=cfg.dry_run)
    plan_id = kentik.get_plan_id(cfg.kentik_plan)
    log.info("Kentik plan: '%s' (id=%s)", cfg.kentik_plan, plan_id)

    if cfg.only:
        log.info("--only %s: running just that phase", cfg.only)

    run_sites = cfg.only in (None, "sites")
    run_devices = cfg.only in (None, "devices")
    run_labels = cfg.only in (None, "labels")

    if run_sites:
        site_cache = sync_sites(kentik, netbox_sites, limit=cfg.limit)
    elif run_devices:
        # Devices still need to resolve existing sites, just without Phase 1's
        # create/update logic running.
        site_cache = {title: meta["id"] for title, meta in kentik.get_sites().items()}
    else:
        site_cache = {}

    if run_devices:
        device_ids = sync_devices(kentik, netbox_devices, site_cache, plan_id, cfg, limit=cfg.limit)
    else:
        # None (rather than {}) signals sync_labels to resolve device IDs
        # itself, after labels are created, if it ends up needing them.
        device_ids = None

    if run_labels:
        sync_labels(kentik, netbox_devices, netbox_roles, netbox_tenants, netbox_tags, device_ids,
                    limit=cfg.limit, skip_assignment=cfg.skip_label_assignment)

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
