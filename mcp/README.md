# sentinel-bigeye (MCP)

Lets agents look at the home cameras through Sentinel-BigEye (the page on http://127.0.0.1:8770).
It talks only to that page, never to the cameras or the camera machine.

| Tool | Does | Changes anything? |
|---|---|---|
| `bigeye_status` | cameras live or not, recording, motion, what the AI sees, Home/Away, GPU/CPU, guard problems | no |
| `bigeye_snapshot(camera)` | the current picture as an image | no |
| `bigeye_events(limit, camera, query)` | recent events with the local AI's descriptions; `query` searches by meaning | no |
| `bigeye_day_story(date)` | the day's written summary | no (saves a report) |
| `bigeye_positions` | the 360's favourites, where it points, locked or not | no |
| `bigeye_go_to(position)` | turn the 360 to a favourite (refused while locked) | yes |
| `bigeye_set_home(home)` | Home / Away | yes |
| `bigeye_record(camera, start)` | start/stop a recording | yes |
| `bigeye_usage` | BigEye's CPU/RAM/GPU use and the guard's actions | no |
| `bigeye_call_home` | today's call-home attempts per camera and kind | no |

Install: `./install.sh`, register in `~/.mcp.json`:
`{"command": "<this dir>/.venv/bin/python", "args": ["<this dir>/server.py"]}`. New sessions get the tools.
