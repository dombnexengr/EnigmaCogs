# 3DXChat status monitor

This Red-DiscordBot cog reads the public 3DXChat status JSON endpoint:

<https://status.3dxchat.net/stats.php>

It is designed to avoid channel spam:

- creates one status embed per configured server;
- edits that message instead of sending a new message every poll;
- polls once per Red instance and shares the result with all configured servers;
- sends alerts only when the monitored state changes;
- optionally shows the state in the Red bot's global presence;
- reports `UNKNOWN` when the status endpoint itself cannot be verified.

## Installation

```text
[p]repo add EnigmaCogs https://github.com/dombnexengr/EnigmaCogs
[p]cog install EnigmaCogs threedxstatus
[p]load threedxstatus
```

## Commands

`[p]3dxstatus` shows the current status in the channel where it is used.

`[p]3dxstatus setup #channel` creates or reuses the single editable status message.

`[p]3dxstatus alerts true` enables one alert per state transition. Use `false` to disable alerts.

`[p]3dxstatus alertchannel #channel` sends transition alerts to a separate channel. Use the command without a channel to return alerts to the status channel.

`[p]3dxstatus presence true` enables the global bot presence. This command is restricted to the Red bot owner.

`[p]3dxstatus now` performs a fresh one-time check.

`[p]3dxstatus disable` stops automatic updates for the current server without deleting the existing message.

The setup, disable, alerts, and alertchannel commands require administrator or Manage Server permissions.
