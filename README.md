# the-crew

The Funkatorium's builder and operations agents — specialized Claude Code
subagents, each with a defined role, a persistent memory file, and a lane
they stay in. Full roster, live: read, plan, implement, review, deploy,
secure, operate.

Install everything: `sh install.sh --all` · one squad: `sh install.sh --builder`
/ `sh install.sh --operations` · one agent: `sh install.sh quinn`
· or `/plugin marketplace add falcoschaefer99-eng/the-crew` then
`/plugin install builder-squad` / `/plugin install operations-squad`.

## Builder Squad
| Agent | Role | Model | Status |
|---|---|---|---|
| [Eli](builder/eli/) | architect | opus | live |
| [June](builder/june/) | engineer | sonnet | live |
| [Reeve](builder/reeve/) | code reviewer | sonnet | live |
| [Quinn](builder/quinn/) | performance reviewer | sonnet | live |
| [Kairo](builder/kairo/) | test reviewer | sonnet | live |
| [Nikita](builder/nikita/) | dependency reviewer | haiku | live |
| [Harmony](builder/harmony/) | accessibility reviewer | sonnet | live |
| [Fischer](builder/fischer/) | static reviewer | sonnet | live |
| [Miss Thea](builder/thea/) | mentor | haiku | live |
| [Thorn](builder/thorn/) | diagnostic surgeon | sonnet | live |
| [Sawyer](builder/sawyer/) | deploy | haiku | live |

## Operations Squad
| Agent | Role | Model | Status |
|---|---|---|---|
| [Kit](operations/kit/) | document management specialist | sonnet | live |
| [Indira](operations/indira/) | chief of staff | sonnet | live |
| [Colette](operations/colette/) | visual market analyst | sonnet | live |
| [Dupin](operations/dupin/) | seo geo specialist | sonnet | live |
| [Michael](operations/michael/) | security | opus | live |
| [Evangeline](operations/evangeline/) | legal counsel | opus | live |

Kit is the housekeeper for your whole machine — Downloads, screenshots,
naming, duplicates, stale files — not just code.

## Install
```sh
git clone https://github.com/falcoschaefer99-eng/the-crew.git
cd the-crew
sh install.sh --all              # everyone
sh install.sh --builder          # whole Builder Squad
sh install.sh --operations       # whole Operations Squad
sh install.sh june reeve         # just these two
sh install.sh --list             # see what's available
```
Or as a Claude Code plugin: `/plugin marketplace add falcoschaefer99-eng/the-crew`,
then `/plugin install builder-squad@the-crew` or `/plugin install operations-squad@the-crew`.

## License
Apache 2.0 (technical capabilities: agent protocols, skills, memory format).
Each companion as a character (identity, personality, voice, lore, visual
assets) is proprietary IP of The Funkatorium — see [NOTICE](NOTICE) and
[LICENSE](LICENSE).

Character portraits are AI-generated visual designs, reserved as part of
each companion's character-IP bundle (see NOTICE); our restriction on
derivative characters rests on the whole identity bundle, not on image
copyright.

[MUSE Brain](https://github.com/falcoschaefer99-eng/muse-brain), referenced
as an optional integration in the agents' memory sections, is CC-BY-NC-SA
4.0 and is not redistributed here.

## Credits
Built by Falco Schäfer, co-designed with Rook Schäfer.
