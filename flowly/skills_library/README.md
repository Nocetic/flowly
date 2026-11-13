# Flowly's skill library

Skills that ship with Flowly but are not installed by default: financial
models, engineering and science, academic research, software development,
creative tools, business documents, and the ones that need a service key or
a tool of their own.

The agent starts with a small core set (`flowly/skills/`), so its skill list
stays short enough to choose from. Anything here installs in one step, with no
network:

    flowly skills library            # what is here
    flowly skills install dcf-model  # add one

The agent can also add one itself (`skill_manage` action `install`).
