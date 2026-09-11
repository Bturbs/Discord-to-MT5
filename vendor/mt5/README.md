Place a clean, broker-provided 64-bit MT5 portable installation here, with
`terminal64.exe` at this directory's root. The proprietary terminal is not bundled.

Prepare a separate copy, not your everyday account's data directory. Start that
copy with `/portable` on Windows, configure the terminal options, then close it:

- Enable Algo Trading and uncheck “Disable automated trading via external Python API”.
- Review the account-switch auto-disable option, because credentials are provided at boot.
- No EAs, saved account credentials, MQL5 account, personal profiles, or trading history.
- Retain the clean terminal preferences and any broker server-discovery files needed
  for the exact `MT5_SERVER`. Test that this clean copy can log in using the API.

Docker excludes known credential/history paths, but this is not a universal credential
scrubber: inspect the template yourself before building. Credentials belong in environment
secrets. Test the prepared terminal under Wine on a demo account; a working Windows copy
does not establish that the same broker build works inside Azure Container Apps.
