# Discord → MT5 signal capture

Discord-to-MT5: used for following HunterFx Discord Signals.

A small Python service that watches messages from Discord bot **1547302271942000751**,
parses one entry/target/stop, calculates volume from a configured percentage of MT5
account equity, and submits a market order with its stop and target attached.

The supplied signal format is implemented directly:

```text
LONG entry 29335.25
🎯 29455.75
🛑 29264.50
```

`SHORT` is supported with reversed stop/target ordering. Text and simple Discord embed
fields are supported. Each allowed channel maps to an exact broker symbol, so MNQ can
be named differently by different brokers. No symbol is guessed from the price.

## Hosting design

```text
Discord Gateway (outbound connection)
    → author + channel filter → strict parser
    → durable message claim + account execution gate
    → broker quote / risk / margin / order validation
    → MT5 market order with SL + TP

Azure Container Apps: one Linux amd64 replica
    └─ Xvfb + Wine + Windows Python + MT5 terminal
Azure Table Storage: message audit and account execution gate
Log Analytics: service logs
```

[Azure Container Apps requires Linux amd64 images](https://learn.microsoft.com/en-us/azure/container-apps/containers).
[MetaQuotes runs MT5 on Linux through Wine](https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux).
The container therefore runs both MT5 and the Windows Python integration under Wine;
installing the MT5 package into Linux Python would not provide terminal IPC.
This container arrangement still needs a broker-specific demo smoke test. If the
broker's terminal proves incompatible with Wine, use a Windows VM for execution and
keep the Discord listener in Container Apps as a subsequent split architecture.

## Configuration

Copy `config.example.toml` to `config.toml` and `.env.example` to `.env`.
The local `.env` file is excluded from Git, the Docker build context and distributed ZIP.
Replace the placeholder channel ID and broker symbol before starting. Example:

```toml
risk_percent = 0.5          # Example: 0.5%, not 50%. Choose your own value.
dry_run = true
allow_real_account = false
max_signal_age_seconds = 60
max_tick_age_seconds = 15
max_positions = 1
magic = 1547302

[channels."YOUR_CHANNEL_ID"]
symbol = "MNQ"             # Replace with the exact Market Watch symbol.
max_entry_drift_points = 20
max_spread_points = 30
deviation_points = 10
commission_per_lot = 0.0   # Account currency, round-trip allowance per MT5 lot.
```

The channel ID must contain digits only. Add another `[channels."..."]` block for
another channel. Config is loaded at startup; changing the baked-in config requires
an image rebuild and a new revision. Do not configure two accounts against the same
local SQLite ledger. Azure automatically partitions records by MT5 server and login.

**“Points” means the broker's MT5 `symbol_info.point`, not an MNQ index point.**
For example, if `point=0.01`, 20 points means a 0.20 price difference. Tune the
spread, drift and deviation values for the broker. Futures symbols may require
expiry/rollover changes in config; there is no automatic rollover.

Required secrets/environment:

| Variable | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | Token for your listener bot, not the signal bot |
| `MT5_LOGIN` | Numeric trading account login |
| `MT5_PASSWORD` | Trading password |
| `MT5_SERVER` | Exact MT5 broker server name |
| `AZURE_STORAGE_CONNECTION_STRING` | Durable Table Storage; mandatory in Azure |
| `MT5_PATH` | Terminal executable path; set by Docker, required for native Windows |

Create/invite a normal Discord bot to the server containing the signal channel. It
needs View Channel and the Message Content Intent in both code and the Developer
Portal. This app reads messages only and requires no Send Messages permission.
[Discord documents content and embed access under Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent).
The source ID must be the message author's bot user ID. Webhook messages are rejected.

## Risk and execution rules

1. Risk budget = current equity × `risk_percent / 100`.
2. Fetch the exact broker symbol, quote, tick grid and volume constraints.
3. Ask MT5 for estimated loss from an adverse entry price to the signal stop,
   using [`order_calc_profit`](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)
   in account currency. Add the configured commission allowance.
4. Round volume **down** to the broker volume step. Reject when the minimum lot
   exceeds budget. Recalculate the final volume's loss and check free margin.
5. Validate spread, entry drift, stop distances, account identity, quote freshness,
   existing exposure, filling policy and `order_check` before submitting.

`dry_run=true` still connects to MT5 and performs the broker checks; it records the
plan without calling `order_send`. It is not a simulated market-data feed. MT5
`order_check` may reject if the broker/session/permissions do not allow the order.

With `dry_run=false`, demo accounts are allowed. A real account additionally requires
`allow_real_account=true`. The default `max_positions=1` counts all open positions
and pending orders on the account, including manual activity. Existing exposure in
the same symbol is always rejected to avoid accidental netting. Use a dedicated
account; this service cannot serialize another EA or a human's orders.

The configured percentage is an **estimated loss at the stop**, not a guaranteed loss
cap. Gaps, slippage and unconfigured fees can exceed it; a broker may ignore the
requested deviation for market/exchange execution. This version uses a single target
and does not scale out, trail stops, move to breakeven, interpret images, process
message edits/cancellations, or place pending limit/stop orders. Extra instructions
or multiple targets are rejected. Reposted signals with new Discord message IDs are
new instructions; deduplication is by original message ID.

## Run on Windows first

Install Python 3.12 x64 and a broker-provided MT5 terminal. Use a separate portable
terminal copy; see `vendor/mt5/README.md`. Enable the terminal's Algo Trading option
and uncheck “Disable automated trading via external Python API” for execution.
[MetaQuotes documents this setting](https://www.metatrader5.com/en/terminal/help/algotrading/trade_robots_indicators).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
Copy-Item config.example.toml config.toml
# Edit config.toml. Set the required secrets in this process's environment.
# The native Python entry point does not automatically load .env.
$env:MT5_PATH = 'C:\path\to\portable-mt5\terminal64.exe'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m signal_capture
```

For an existing non-portable terminal set `MT5_PORTABLE=false` explicitly. No real
terminal is opened by the tests; all broker calls in tests use a fake adapter.

## Build and run Docker

1. Populate `vendor/mt5/` with the clean, prepared broker terminal described in its
   README. No proprietary MT5 binaries or credentials are supplied with this project.
2. Configure `config.toml` and `.env`, leaving dry-run enabled for initial validation.
3. Start Docker Desktop in Linux container mode, then run:

```powershell
docker compose build
docker compose up -d
docker compose logs -f
curl.exe http://localhost:8080/health/ready
```

The image deliberately fails to build if `terminal64.exe` is absent. It downloads
Windows Python 3.12.10 from python.org and Wine from WineHQ. The app runs as an
unprivileged user, with an Xvfb virtual display. No remote desktop or trading HTTP API
is exposed. Compose maps the health port to localhost only. Local SQLite audit lives
on a Docker volume. Keep that volume across restarts; `docker compose down -v` would
delete it and remove duplicate protection. Avoid multiple Compose replicas.

## Deploy to Azure Container Apps

Prerequisites: Azure CLI with Container Apps/Bicep support, an Azure subscription,
and a role that can create resources and assign AcrPull. The template creates the
Container Apps environment, app, managed identity, Log Analytics and Storage. It
expects an existing ACR in the same resource group with normal RBAC registry permissions
(the supplied AcrPull assignment does not cover an ABAC-only registry setup).

After completing the local demo validation, the following commands create billable
Azure resources. Set your own resource group, registry name, region and immutable tag:

```powershell
az login
$env:AZURE_RESOURCE_GROUP = 'rg-signal-capture'
$env:ACR_NAME = 'yourgloballyuniqueregistry'
$env:IMAGE_TAG = 'signal-capture:20260911-1'
az group create --name $env:AZURE_RESOURCE_GROUP --location uksouth
az acr create --resource-group $env:AZURE_RESOURCE_GROUP --name $env:ACR_NAME --sku Basic
az acr build --registry $env:ACR_NAME --image $env:IMAGE_TAG --platform linux/amd64 .
# Set DISCORD_BOT_TOKEN, MT5_LOGIN, MT5_PASSWORD and MT5_SERVER securely in your environment.
az deployment group what-if --resource-group $env:AZURE_RESOURCE_GROUP --parameters infra/main.bicepparam
az deployment group create --resource-group $env:AZURE_RESOURCE_GROUP --parameters infra/main.bicepparam
az containerapp logs show --resource-group $env:AZURE_RESOURCE_GROUP --name signal-capture --follow
```

`main.bicepparam` reads secrets from environment variables; do not commit secret
parameter files. The app receives secrets through Container Apps secret references.
ACR pulls use managed identity; initial role propagation can require a deployment retry.

Azure uses **single revision mode, minReplicas=1, maxReplicas=1, 2 vCPU/4 GiB and no
ingress**. Keep the minimum replica count at one for the persistent Discord Gateway
connection. The first resource allocation is a starting point to measure, not a tested
capacity requirement. For a broker with IP restrictions, add a VNet/NAT design; this
template does not promise a fixed outbound IP.

Azure Table Storage records survive replacement replicas. A permanent account gate
serializes order handling even during revision overlap. The same storage account and
partition must be retained when redeploying. A different storage account loses access
to previous claims. Do not automatically expire or delete audit records.

## Recovery and operating limits

Each message moves through `CLAIMED` → `DRY_RUN`, `REJECTED`, or `SUBMITTING` →
`EXECUTED`. Before a send, `SUBMITTING` is durably recorded. A crash, uncertain broker
response, or storage write failure leaves the account gate locked. Later signals
wait only until their freshness deadline and are then skipped with an error log.
There is no automatic retry of an ambiguous order, including after restarts.

If the gate is left locked:

1. Stop **every** app/replica using the account; ensure no broker submission remains
   in flight. Read the gate and its message record with the commands below.
2. Reconcile the broker's active orders, positions, order history and deal history
   for the message's `dc:<message-id>` comment, magic, symbol, quantity and time.
   Broker comments can change, so do not rely on the comment alone.
3. After resolving the outcome, release only the gate with its exact owner token.
   Keep the message record. Resume the app; the message will not be replayed.

```powershell
python -m signal_capture.ops show
python -m signal_capture.ops show --message-id 1234567890123456789
python -m signal_capture.ops release-gate --expected-owner TOKEN_FROM_SHOW --all-replicas-stopped-and-broker-reconciled
```

Run these with the same MT5 server/login and storage connection as the deployment.
The release command is explicitly an offline maintenance operation. There is no
exactly-once guarantee across MT5 and storage; the design sacrifices automatic
recovery when execution is uncertain to avoid blind resubmission. Partial fills are
recorded and never automatically topped up. A broker response other than completed
or partially completed is conservatively held for reconciliation.

Discord reconnects through the library, but there is no history backfill. Signals
missed during downtime, queue overflow or a locked gate may be lost. Edited messages
are ignored. Connect and validate before expecting capture. Dry-run claims remain
claimed when switching execution on, so old dry-run signals never become live orders.

Health probes: `/health/live` checks worker progress; `/health/ready` additionally
requires Discord readiness, MT5 account health and an unlocked account gate. Configure
Azure alerts for unready replicas, restarts, and log messages containing `halted`,
`gate`, or `dropped`. Readiness alone does not stop an outbound worker; the gate and
execution checks enforce the halt. Alerts are not provisioned by this template.

## Validation status

66 Python tests pass, covering parsing, risk rounding, broker guards, durable duplicates,
concurrent claims, submission ordering and uncertain outcomes. Dependency checks pass.
The Azure template compiles with the official Bicep CLI 0.47.16. Container startup, Wine compatibility, Azure provisioning,
Discord capture and demo/live execution require testing with your supplied terminal
and account. Nothing has been deployed or traded as part of generating this project.
