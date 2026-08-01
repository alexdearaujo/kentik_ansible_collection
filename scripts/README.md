# netbox_sync.py

Syncs NetBox devices, sites, and labels into Kentik. This is a standalone script: it
talks to both APIs directly over HTTP and does not depend on, or call into, the
Ansible modules in this collection.

## What it does

The script runs in three phases, in order:

1. **Sites**: for every NetBox site, create it in Kentik if it's missing. If it
   already exists and NetBox has current lat/lon values that differ from what's in
   Kentik, update it (NetBox is treated as the source of truth for coordinates, but
   only when NetBox actually has a value; a NetBox site with no lat/lon set is left
   alone rather than zeroing out a real value already configured in Kentik). The
   site's `addressClassification.userAccessNetworks` is kept in sync the same way,
   from NetBox prefixes; see [Site userAccessNetworks](#site-useraccessnetworks). The
   Kentik site title itself is configurable; see [Site naming](#site-naming).
2. **Devices**: create or update every NetBox device in Kentik, resolving each
   device's site, primary IP, and (optionally) NMS agent configuration. Before
   updating an existing device, its current Kentik state is fetched and compared
   field by field so the log line for that update names exactly what's changing;
   see [Device update visibility](#device-update-visibility).
3. **Labels**: create Kentik labels from NetBox device roles, tenants, and tags,
   then assign the relevant labels to each device. Which of those three sources are
   used, and whether each one's name or slug becomes the label text, is configurable;
   see [Label sources](#label-sources).

### NMS agent detection

If a device carries a NetBox tag named `kentik_primary_agent=<agentId>`, it's flagged
for Kentik NMS monitoring and `<agentId>` is used as the agent ID. This requires the
device to also have a primary IP set. If the tag is present but there's no IP, the
script logs a warning and skips NMS configuration for that device (it still syncs the
device itself).

### Device update visibility

When a NetBox device already exists in Kentik, the script fetches its current state
before updating it and compares each field it manages (`deviceDescription`,
`deviceSubtype`, `deviceSampleRate`, `deviceBgpType`, `minimizeSnmp`, `sendingIps`,
`deviceSnmpIp`, `deviceSnmpCommunity`, site, and plan) against what this run would
send. The log line for that update spells out exactly which fields differ:

```text
Updating device rtr1 (id=410680): deviceDescription 'old desc' -> 'new desc'; siteId 12 -> 36515
```

If nothing actually differs, the line says `(no field changes detected)` instead
(the update call still happens either way; this only changes what's logged). NMS
agent configuration can't be compared this way, since Kentik doesn't echo it back
in the same shape it's written in, so it's only flagged as `NMS agent config
included (not diffed)` rather than diffed field by field.

### Site naming

By default the Kentik site title is just the NetBox site's plain `name`, unchanged.
`--site-name-template` (or `KENTIK_SITE_NAME_TEMPLATE`) overrides this with a
`str.format` template over a fixed set of NetBox site fields:

- `{name}`, `{slug}`; the site's own fields
- `{facility}`; the site's facility string
- `{region}`, `{group}`, `{tenant}`; the *name* of the site's region/group/tenant
  (e.g. "North Carolina")
- `{region_slug}`, `{group_slug}`, `{tenant_slug}`; the *slug* of the same
  (e.g. "us-nc")

(`region`/`group`/`tenant` are NetBox foreign keys; a site with none set has both
the name and slug variant empty for that field.)

```bash
uv run --env-file .env python scripts/netbox_sync.py --site-name-template "{region}-{name}"
# or, using the slug instead of the full region name:
uv run --env-file .env python scripts/netbox_sync.py --site-name-template "{region_slug}-{name}"
```

A NetBox site named "Riverside" in region "North Carolina" (slug `us-nc`) becomes
Kentik site "North Carolina-Riverside" with the first template, or "us-nc-Riverside"
with the second. Both variants are always available, so switching between them (or to
`{group}`/`{tenant}` instead of `{region}` entirely) is a config change, not a code
change.

If a site is missing a field the template references (e.g. no region set), that one
site falls back to its plain NetBox name instead of producing a broken title like
"-Riverside"; sites with the field set still get the full template. A template
referencing a field outside the supported set above is rejected at startup with a
clear error, so a typo doesn't silently produce blank/wrong names across a whole run.

This is a config change, not a code change, specifically so the naming scheme can be
adjusted (or reverted to the plain name) without touching the script.

### Site userAccessNetworks

A site's Kentik `addressClassification.userAccessNetworks` is populated from NetBox:
every NetBox prefix with status `container` that is scoped directly to that site
(not to a region, site group, or location) contributes its CIDR to the list. This is
computed and kept in sync on every run, the same way lat/lon is: a brand-new site is
created with the current list, and an existing site whose network list has drifted
(a container prefix was added, removed, or its site changed) is updated to match.
`infrastructureNetworks` and `otherNetworks` are never touched by this script.

### Label sources

By default, Phase 3 creates a Kentik label for every NetBox device role, tenant, and
tag (by slug) and assigns the relevant ones to each device. `--label-sources` (or
`KENTIK_LABEL_SOURCES`) overrides which of those three are used, and whether each
one's `name` or `slug` becomes the label text, as comma-separated `source:field`
pairs:

```bash
# Default (explicit): role, tenant, and tag labels, all keyed by slug
uv run --env-file .env python scripts/netbox_sync.py --label-sources "role:slug,tenant:slug,tag:slug"

# Only role and tenant labels; tags are never created or assigned
uv run --env-file .env python scripts/netbox_sync.py --label-sources "role:slug,tenant:slug"

# Role by slug, tenant by its full name instead
uv run --env-file .env python scripts/netbox_sync.py --label-sources "role:slug,tenant:name"
```

A source left out of the spec entirely (like `tag` in the second example) is skipped
completely: no label is created for it, and no device is scanned for it during
assignment. This is a config change, not a code change, so which sources are used
and how they're labeled can be adjusted without touching the script. A spec naming an
unsupported source (only `role`, `tenant`, and `tag` are valid) or field (only `name`
and `slug` are valid) is rejected at startup with a clear error.

Every label's display text is prefixed with its source type, so a device role
`core` becomes the Kentik label `role:core`, tenant `Acme Corp` becomes
`tenant:Acme Corp`, and tag `prod` becomes `tag:prod`. This keeps labels from
different sources from ever colliding (e.g. a role and a tag that happen to share
a slug) and makes each label's origin obvious at a glance in Kentik.

## Requirements

This repo uses [uv](https://docs.astral.sh/uv/) to manage the Python environment.
From the repo root:

```bash
uv sync
```

This installs `requests` and the other pinned dependencies into `.venv`.

## Authentication

### Kentik

- `KENTIK_EMAIL` / `--kentik-email`
- `KENTIK_TOKEN` / `--kentik-token`

Sent as the legacy `X-CH-Auth-Email` + `X-CH-Auth-API-Token` headers (same scheme
used by the other modules in this collection).

### NetBox

- `NETBOX_TOKEN` / `--netbox-token`: accepts **either** token format NetBox issues,
  auto-detected:
  - **v1** (legacy, deprecated as of NetBox 4.6, removed in 5.0): a plain token,
    sent as `Authorization: Token <token>`.
  - **v2** (current): a token in the form `nbt_<key>.<secret>`, sent as
    `Authorization: Bearer <token>`. Recognized by the `nbt_` prefix, so no extra
    configuration is needed; just paste whichever token NetBox gave you.

## Configuration reference

All options can be set via environment variable or CLI flag; the CLI flag always
wins if both are given.

| Env var | CLI flag | Required | Description |
| --- | --- | --- | --- |
| `KENTIK_EMAIL` | `--kentik-email` | Yes | Kentik API email |
| `KENTIK_TOKEN` | `--kentik-token` | Yes | Kentik API token |
| `NETBOX_URL` | `--netbox-url` | Yes | NetBox base URL, e.g. `https://netbox.example.com` |
| `NETBOX_TOKEN` | `--netbox-token` | Yes | NetBox API token (v1 or v2, see above) |
| `KENTIK_PLAN_NAME` | `--kentik-plan` | Only when the devices phase runs (default full run, or `--only devices`) | Name of an existing Kentik plan to assign synced devices to |
| `KENTIK_REGION` | `--kentik-region` | No (default `US`) | `US` or `EU` |
| `KENTIK_SNMP_COMMUNITY` | `--snmp-community` | No | SNMP community string applied to every synced device |
| `KENTIK_SNMP_CRED` | `--snmp-credential` | No (default `default`) | SNMP credential name used for NMS-tagged devices |
| `KENTIK_SAMPLE_RATE` | `--sample-rate` | No (default `1`) | Flow sample rate applied to every synced device |
| `DRY_RUN` | `--dry-run` | No | Plan without applying changes; see [Dry-run mode](#dry-run-mode) |
| `KENTIK_SYNC_LIMIT` | `--limit` | No | Cap mutations per phase; see [Limit mode](#limit-mode) |
| `KENTIK_SYNC_ONLY` | `--only` | No | Run a single phase; see [Running a single phase](#running-a-single-phase) |
| `NETBOX_INSECURE_TLS` | `--netbox-insecure-tls` | No | Skip TLS verification on NetBox requests (lab/self-signed instances only) |
| `KENTIK_SITE_NAME_TEMPLATE` | `--site-name-template` | No (default `{name}`) | Kentik site title template; see [Site naming](#site-naming) |
| `KENTIK_LABEL_SOURCES` | `--label-sources` | No (default `role:slug,tenant:slug,tag:slug`) | Which NetBox objects become labels, and name vs. slug; see [Label sources](#label-sources) |
| `KENTIK_SYNC_SITE` | `--site` | No | Scope the run to one NetBox site (exact name match); see [Scoping to a single site](#scoping-to-a-single-site) |

Boolean env vars (`DRY_RUN`, `NETBOX_INSECURE_TLS`) accept `1`, `true`, `yes`, or `on`
(case-insensitive); anything else is treated as false.

## Basic usage

```bash
uv run --env-file .env python scripts/netbox_sync.py
```

`--env-file` points `uv run` at a `.env` file holding the config above so you don't
have to export everything by hand. A typical `.env`:

```dotenv
KENTIK_EMAIL=you@example.com
KENTIK_TOKEN=xxxxxxxxxxxxxxxx
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=nbt_xxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KENTIK_PLAN_NAME=My Plan
```

Equivalent using CLI flags instead of a `.env` file:

```bash
uv run python scripts/netbox_sync.py \
  --kentik-email you@example.com \
  --kentik-token xxxxxxxxxxxxxxxx \
  --netbox-url https://netbox.example.com \
  --netbox-token nbt_xxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --kentik-plan "My Plan"
```

## Dry-run mode

```bash
uv run --env-file .env python scripts/netbox_sync.py --dry-run
```

With `--dry-run`, every read (`GET`) call against NetBox and Kentik still happens,
so the plan reflects real current state, but every mutating call (site/device/label
create, device update, label assignment) is logged instead of sent:

```json
2026-07-30 13:19:32,900 INFO [DRY-RUN] Would create device [site 'Butler Communications' is itself pending creation; placeholder siteId=-1]: {
  "deviceBgpType": "none",
  "deviceDescription": "Synced from NetBox",
  "deviceName": "test-router-1",
  "deviceSampleRate": 1,
  "deviceSubtype": "router",
  "minimizeSnmp": false,
  "planId": 125757,
  "siteId": -1
}
```

Resources that would be newly created (a site, a device, a label) are tracked
in-memory with a placeholder negative ID, so later log lines that reference them
(e.g. a device pointing at a site that's itself only "pending creation" this run)
still read coherently instead of crashing or showing a blank ID. Negative IDs are
never real Kentik IDs; if you see one, it means "this doesn't exist yet, dry-run
made it up so the rest of the plan could be logged."

Always run with `--dry-run` first against a new NetBox/Kentik pairing before running
for real.

## Limit mode

```bash
uv run --env-file .env python scripts/netbox_sync.py --dry-run --limit 5
```

`--limit N` caps the number of mutating operations performed **per phase** in a
single run:

- at most `N` sites created or updated,
- `N` devices processed (created or updated),
- `N` new labels created,
- `N` devices whose label assignments are touched.

Each phase gets its own independent budget of `N`; it's not one shared counter
across the whole run. Items that don't require a mutation (an already-existing site
with matching lat/lon, a device with no labels to assign) don't consume the budget.

This is meant for smoke-testing a change against a small, cheap subset before
running it against your full inventory. Combine it with `--dry-run` for the safest
possible first look:

```bash
uv run --env-file .env python scripts/netbox_sync.py --dry-run --limit 3
```

## Running a single phase

```bash
uv run --env-file .env python scripts/netbox_sync.py --only labels
```

`--only {sites,devices,labels}` runs just one phase instead of the full
sites -> devices -> labels pipeline. NetBox data is always fetched in full regardless
of `--only` (except for any narrowing `--site` does; see
[Scoping to a single site](#scoping-to-a-single-site)); only which Kentik-side
phase(s) actually run is affected.

- **`--only sites`**: create/update sites only. Nothing else runs.
- **`--only devices`**: create/update devices only. Existing sites are still looked
  up (read-only) to resolve each device's `siteId`, but Phase 1's create/update logic
  does not run, so a device whose NetBox site doesn't exist in Kentik yet will still
  be skipped with a warning.
- **`--only labels`**: create labels and assign them to devices only. Since Phase 2
  didn't just run, each device's Kentik ID is resolved with an individual lookup
  (one extra API read per NetBox device) instead of reusing Phase 2's in-memory
  result. Devices not yet present in Kentik are skipped with a warning; run
  `--only devices` (or a full run) first if you need them created.

`KENTIK_PLAN_NAME` / `--kentik-plan` is only required when the devices phase
actually runs (a default full run, or `--only devices`); it can be omitted for
`--only sites` or `--only labels`.

Useful when you've already synced sites and devices and only want to pick up new
NetBox tags/roles/tenants as labels, without re-touching everything else:

```bash
uv run --env-file .env python scripts/netbox_sync.py --only labels --dry-run
uv run --env-file .env python scripts/netbox_sync.py --only labels
```

## Scoping to a single site

```bash
uv run --env-file .env python scripts/netbox_sync.py --site "DM-Akron"
```

`--site <name>` (or `KENTIK_SYNC_SITE`) restricts the run to one NetBox site
(exact name match, case sensitive) instead of every site. Only that site, and the
devices and container prefixes scoped to it, are fetched from NetBox and synced;
role/tenant/tag labels are still created from the full NetBox inventory (that's
cheap and idempotent either way), but assignment only ever touches that site's
devices. If the named site doesn't exist in NetBox, the script exits immediately
with a clear error instead of silently syncing nothing.

Composes with `--only`, e.g. sync just one site's devices, assuming the site
itself is already in Kentik:

```bash
uv run --env-file .env python scripts/netbox_sync.py --site "DM-Akron" --only devices
```

## Skipping label assignment

```bash
uv run --env-file .env python scripts/netbox_sync.py --only labels --skip-label-assignment
```

`--skip-label-assignment` creates labels in Kentik as usual (from whichever sources
`--label-sources` selects) but never assigns them to any device. It also skips
resolving device IDs entirely, since that work only exists to support assignment.
Useful for making sure the label set exists in Kentik without touching any device.

## More examples

Point at the EU region and skip TLS verification against an internal lab NetBox:

```bash
uv run --env-file .env python scripts/netbox_sync.py \
  --kentik-region EU \
  --netbox-insecure-tls
```

Set an SNMP community for every synced device, and a custom flow sample rate:

```bash
uv run --env-file .env python scripts/netbox_sync.py \
  --snmp-community public \
  --sample-rate 10
```

Dry-run just the sites phase, capped to 10, before touching anything else:

```bash
uv run --env-file .env python scripts/netbox_sync.py --dry-run --only sites --limit 10
```

Full production run, no limits, no dry-run (do this only after a clean dry-run):

```bash
uv run --env-file .env python scripts/netbox_sync.py
```

Override one value from `.env` on the command line without editing the file (CLI
flags always win over env vars):

```bash
uv run --env-file .env python scripts/netbox_sync.py --kentik-plan "Staging Plan"
```

## Per-item failures

If a single site, device, or label create/update/assignment fails (e.g. Kentik rejects
one bad value, or that one call runs out of network retries), the script logs the full
raw error immediately and keeps going with everything else instead of aborting the
whole run. Every failure is listed in a summary table printed at the end, with the
reason reduced to just the status code and the API's own error message (instead of the
raw `HTTP POST <url> returned 400: {"code":3,"message":"...","details":[]}` wrapper),
so it's actually readable instead of getting truncated mid-JSON:

```text
┌─────────────────┬─────────┬─────────────────────────────────────────────────────────────┐
│ Phase            │ Item    │ Reason                                                       │
├─────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ devices          │ rtr9    │ 400: ValidationError: Device name (rtr9) Already Exists      │
│ labels: assign   │ rtr14   │ HTTP PUT https://.../labels failed after 3 retries           │
└─────────────────┴─────────┴─────────────────────────────────────────────────────────────┘
```

The script exits with status `1` whenever at least one item failed, so it's safe to
gate CI/cron on the exit code. Since every phase is idempotent, re-running the same
command only retries what actually failed; everything that already succeeded is
detected as up to date and left alone.

## Tests

```bash
uv run pytest
```

The suite in `scripts/tests/test_netbox_sync.py` covers pure helpers, the Kentik/
NetBox HTTP clients (including dry-run gating and the 404 guards), phase
orchestration and `--limit`/`--only` behavior, and a handful of end-to-end scenarios
driven against mocked HTTP (via `requests-mock`) rather than real credentials.

## Troubleshooting

- **`Missing required config: ...`**: one of `KENTIK_EMAIL`, `KENTIK_TOKEN`,
  `NETBOX_URL`, `NETBOX_TOKEN` isn't set, or `KENTIK_PLAN_NAME` isn't set and the
  devices phase is running (default full run, or `--only devices`). Check your
  `.env` or CLI flags.
- **`NetBox GET ... returned 403: {"detail":"Authentication credentials were not
  provided."}` / `"Invalid v1 token"`**: check `NETBOX_TOKEN` is current. NetBox
  demo/shared instances in particular tend to rotate or expire tokens.
- **`Kentik plan '...' not found`**: `KENTIK_PLAN_NAME` must exactly match an
  existing plan name in your Kentik org.
- **A device is skipped with `site '...' not found in Kentik`**: that device's
  NetBox site hasn't been synced to Kentik yet. Run the sites phase first (a full run,
  or `--only sites`), or check the site name matches exactly.
- **A device is skipped with `has no name in NetBox`**: NetBox allows `name: null`
  for non-master members of a virtual chassis. These are intentionally skipped since
  Kentik requires a device name.
- **Network error / `Max retries exceeded`**: a transient failure talking to NetBox
  or Kentik is retried up to 3 times with a short back-off before the script exits
  with a clean error message. It is always safe to re-run the same command; every
  phase is idempotent, so already-synced resources are left alone.
