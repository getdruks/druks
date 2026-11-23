## Verification profile

{% if not has_commands -%}
No lint / typecheck / test / smoke commands are configured for this repo. Take verification conventions from the repo's AGENTS.md if it has one; otherwise verification is acceptance-criteria-driven. Never invent commands (pytest, npm test, …) the project doesn't actually use.
{% else -%}
{% for section in sections if section.command_entries -%}
**{{ section.label }}:**
{% for entry in section.command_entries -%}
- `{{ entry.command }}`{{ " — CI: `" ~ entry.ci_check ~ "`" if entry.ci_check else "" }}
{% endfor %}
{% endfor -%}
{% endif -%}
