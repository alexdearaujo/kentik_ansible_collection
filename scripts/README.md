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
   alone rather than zeroing out a real value already configured in Kentik).
2. **Devices**: create or update every NetBox device in Kentik, resolving each
   device's site, primary IP, and (optionally) NMS agent configuration.
3. **Labels**: create Kentik labels from NetBox device roles, tenants, and tags,
   then assign the relevant labels to each device.

### NMS agent detection

If a device carries a NetBox tag named `kentik_primary_agent=<agentId>`, it's flagged
for Kentik NMS monitoring and `<agentId>` is used as the agent ID. This requires the
device to also have a primary IP set. If the tag is present but there's no IP, the
script logs a warning and skips NMS configuration for that device (it still syncs the
device itself).

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
| `KENTIK_PLAN_NAME` | `--kentik-plan` | Yes | Name of an existing Kentik plan to assign synced devices to |
| `KENTIK_REGION` | `--kentik-region` | No (default `US`) | `US` or `EU` |
| `KENTIK_SNMP_COMMUNITY` | `--snmp-community` | No | SNMP community string applied to every synced device |
| `KENTIK_SNMP_CRED` | `--snmp-credential` | No (default `default`) | SNMP credential name used for NMS-tagged devices |
| `KENTIK_SAMPLE_RATE` | `--sample-rate` | No (default `1`) | Flow sample rate applied to every synced device |
| `DRY_RUN` | `--dry-run` | No | Plan without applying changes; see [Dry-run mode](#dry-run-mode) |
| `KENTIK_SYNC_LIMIT` | `--limit` | No | Cap mutations per phase; see [Limit mode](#limit-mode) |
| `KENTIK_SYNC_ONLY` | `--only` | No | Run a single phase; see [Running a single phase](#running-a-single-phase) |
| `NETBOX_INSECURE_TLS` | `--netbox-insecure-tls` | No | Skip TLS verification on NetBox requests (lab/self-signed instances only) |

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
of `--only`; only which Kentik-side phase(s) actually run is affected.

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

Useful when you've already synced sites and devices and only want to pick up new
NetBox tags/roles/tenants as labels, without re-touching everything else:

```bash
uv run --env-file .env python scripts/netbox_sync.py --only labels --dry-run
uv run --env-file .env python scripts/netbox_sync.py --only labels
```

## Skipping label assignment

```bash
uv run --env-file .env python scripts/netbox_sync.py --only labels --skip-label-assignment
```

`--skip-label-assignment` creates role/tenant/tag labels in Kentik as usual but never
assigns them to any device. It also skips resolving device IDs entirely, since that
work only exists to support assignment. Useful for making sure the label set exists
in Kentik without touching any device.

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
  `NETBOX_URL`, `NETBOX_TOKEN`, `KENTIK_PLAN_NAME` isn't set. Check your `.env` or
  CLI flags.
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
