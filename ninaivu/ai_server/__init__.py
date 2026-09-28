"""Image edits run on a ComfyUI server elsewhere in the house.

The Ninaivu machine is often a small always-on box; the models that do
convincing edits want a graphics card with 16–24 GB. This package lets an
administrator point Ninaivu at a ComfyUI instance on the home network and pick
which of its workflows serves each Playground job. Nothing is sent anywhere
until that is configured, and then only to that one address.
"""
