# AI server: image edits on a ComfyUI PC

Ninaivu can send the Playground's heavy image jobs — **Generative edit**,
**Object removal**, **Paint and ask (inpaint)**, **Upscale**, **Restore faces** and **Colourise** — to a
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) server on another computer
in the house, typically a PC with a graphics card.
Everything else (search, faces, adjustments, background removal) stays on the
Ninaivu machine.

This is part of the **Creative Studio extension** (`extensions/creative-studio`),
not the core: install it and switch it on under **System → Settings → Extensions**,
restart, and the **AI server** page appears under AI.

Nothing is sent until an administrator sets it up there, switches it on, and
assigns a workflow to a job. A job with no workflow keeps using the Ninaivu
machine, exactly as before.

## What leaves the Ninaivu machine

- For each request: a preview of the photo being edited (by default up to
  1,024 px on the long side), the request text, the seed and the "avoid" text,
  and for object removal the painted mask.
- It goes only to the address in the console. Ninaivu does not follow redirects,
  and the console warns if the address is outside your home network.
- Ninaivu's request filter runs before anything is uploaded. **What the models on
  the server will produce is decided by the workflow you install**; the local
  model's built-in safety checker does not run there.

## 1. Set up the ComfyUI PC

1. Install ComfyUI and the models you want, and check the workflow works in
   ComfyUI's own interface first.
2. Start ComfyUI so other machines can reach it:

   ```bash
   python main.py --listen 0.0.0.0 --port 8188
   ```

3. ComfyUI has **no sign-in**. Anyone who can reach that port can use the GPU
   and read what it produced. Allow only the Ninaivu machine through the PC's
   firewall. On Windows, in an administrator PowerShell on the ComfyUI PC
   (replace the address with the Ninaivu machine's):

   ```powershell
   New-NetFirewallRule -DisplayName "ComfyUI from Ninaivu" -Direction Inbound -Protocol TCP -LocalPort 8188 -RemoteAddress 192.168.1.20 -Action Allow
   ```

   Never forward port 8188 on your router.

Choosing models: instruction-following editors such as Qwen-Image-Edit or
FLUX.1 Kontext [dev] give far better generative edits than the small local
model, and generally want a graphics card with 16–24 GB of memory (less with
quantised versions). Check each model's licence — some are for non-commercial
use only — and its hardware needs before installing.

## 2. Prepare a workflow

Ninaivu fills in a workflow you built in ComfyUI; it does not design one.

1. In ComfyUI, open the working workflow and use **Workflow → Export (API)**.
   The editor's normal *Save* format is not accepted — the console will say so.
2. Open the exported JSON in a text editor and replace the values Ninaivu should
   fill with placeholders:

   | Placeholder | Replace | Required for |
   | --- | --- | --- |
   | `{{image}}` | the `image` filename in the **Load Image** node | both jobs |
   | `{{prompt}}` | the request text (may sit inside longer text, e.g. `"a photo, {{prompt}}"`) | Generative edit |
   | `{{mask}}` | the `image` filename in a **Load Image (as Mask)** node, with channel `red` | Object removal |
   | `{{negative_prompt}}` | the negative/"avoid" text | optional |
   | `{{seed}}` | the sampler's `seed` (must be the whole value) | optional |

   **Upscale**, **Restore faces** and **Colourise** workflows need only
   `{{image}}`. Their result keeps the size the workflow produces (an upscale
   comes back larger), up to 8,192 px on the long side.

   The mask Ninaivu sends is black with **white where the object was painted** —
   the area to replace.
3. The workflow must end in a **Save Image** (or **Preview Image**) node.
   Ninaivu returns its first image — for edits and object removal, resized to the
   preview's size.

## 3. Connect Ninaivu

In **Admin → AI server**:

1. Enter the address, e.g. `http://192.168.1.50:8188`, and press **Test
   connection**. It shows the graphics card, its memory and the ComfyUI version.
2. **Save** the address.
3. Under **Workflows → Add a workflow**, name it, choose its job, and paste the
   JSON or choose the file. After a connection test, each workflow lists any
   node types the server does not have installed.
4. Under **Jobs**, pick the workflow for each job, adjust the preview size and
   timeout if needed, and **Save jobs**.
5. Tick **Use the AI server for assigned jobs**.

The Playground then tells family members that their preview goes to the AI
server on the home network. Jobs run in the background: the Playground shows how
many jobs are ahead in the server's queue, then the percentage done (read from
ComfyUI's WebSocket, or from its queue when the WebSocket is unavailable).
**Upscale**, **Restore faces** and **Colourise** are under *Enhance tools* in
Sudar's magic tools; a tool with a workflow assigned runs on the AI server, one
without runs on the local model if that is downloaded, and one with neither is
greyed out with a line saying what it needs.

## Troubleshooting

| Message | What to do |
| --- | --- |
| *Cannot reach the AI server… --listen* | ComfyUI is not running, not started with `--listen`, or the firewall blocks the Ninaivu machine. |
| *The AI server rejected the workflow: … node N (UNETLoader): Value not in list* | A model file named in the workflow is not installed on the server, or has a different name. |
| *The server does not have: SomeNode* (console) | Install that custom node on the ComfyUI PC and restart ComfyUI. |
| *did not finish within N seconds, so the job was cancelled* | The GPU is busy or the model is slow; raise **Give up after**, or lower **Preview size sent**. |
| *answered with a redirect* | Use ComfyUI's direct address rather than one behind a redirecting proxy. |
| *already working on two edits from this Ninaivu* | Ninaivu sends at most two jobs at once; wait for one to finish. |

Settings are stored in `config.json` in the state folder (`ai_server_url`,
`ai_server_enabled`, `ai_server_edit_workflow`, `ai_server_remove_workflow`,
`ai_server_timeout`, `ai_server_max_side`); workflows are in
`ai-server/workflows/` there.
