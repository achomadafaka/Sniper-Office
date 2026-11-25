"""The markets plug-in: paper-only prediction-market, perps, stocks and news lanes for a seat. Name it in a seat config:

    "plugins": ["sniper_office.markets"], "tool_groups": ["polymarket", "perps", "stocks", "news"]      # any of the four

Host processes (not seats): `python3 -m sniper_office.markets.{polymarket,perps,stocks,news,sports,intel_hl,books} --config X.json [--once]`. Config keys and tables are in docs/MARKETS.md.
Paper only: no venue key, no order to any venue, no signing."""


def register(host):
    from . import api, tools as T, wakes as WK      # imported here so `python3 -m sniper_office.markets.<process>` does not import its own module first
    for group, name, description, properties, required, handler in T.TOOLS:
        host.add_tool(name, description, properties, required, handler, group=group)
    WK.register(host)
    api.register_panels(host)
