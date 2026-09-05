#!/usr/bin/env python3
"""Robinhood Agentic Trading MCP adapter — the live-execution boundary.

Robinhood shipped an *official* agentic-trading product (announced 2026-05-27):
an MCP server your AI agent connects to, which can read your accounts and place
orders inside a dedicated, isolated "Agentic account". This is the sanctioned
replacement for the deprecated, reverse-engineered robin-stocks path.

    Endpoint:  https://agent.robinhood.com/mcp/trading   (HTTP transport)

Connect from Claude Code:
    claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading

Other clients: Claude Desktop (Settings > Connectors > Add custom connector),
ChatGPT (Developer Mode > Apps), Cursor (Tools & MCPs), Codex
(`codex mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading`).
See: https://robinhood.com/us/en/support/articles/agentic-trading-overview/

------------------------------------------------------------------------------
WHY THIS MODULE IS A STUB (read before wiring live)
------------------------------------------------------------------------------
There are two distinct ways the Robinhood MCP can drive orders, and this repo is
designed for the safer one:

1. Agent-driven (recommended). Claude Code (or another MCP client) holds the
   Robinhood connection. The Python pipeline in this repo (executor.py) is a
   *proposal engine*: it vets signals against guardrails.json and emits a vetted
   list of orders. A human — or Claude with the MCP connected — reviews that
   list and places the orders interactively. The cron never touches money.

2. Programmatic (advanced, not implemented). A headless process speaks the MCP
   protocol to the HTTP endpoint directly, using an OAuth token. That requires:
     - Agentic Trading access granted to your account (still rolling out;
       Robinhood emails you when you're in).
     - An OAuth flow + token storage for the agent account.
     - An MCP client implementation (the `mcp` Python SDK, or raw JSON-RPC).
   None of that is wired here on purpose: live, unattended, LLM-initiated
   trading is the highest-risk configuration and should be opt-in, reviewed,
   and built only once you've watched the proposal loop behave for a while.

3. Bridge (LIVE since 2026-09-05 — see live_bridge.py + routines/live-execution.md).
   The middle path. executor.py stays a proposal engine (this module stays a
   stub, so an in-process live run can never place anything). A Claude session
   that holds the Robinhood MCP runs `live_bridge.py`, which re-vets each
   fresh proposal against a snapshot of the REAL account (buying power,
   positions, open orders, tradability) and emits an exact order list with
   deterministic ref_ids; the session places those via the MCP
   (review_equity_order -> place_equity_order) and records every order id and
   fill back through the CLI into the committed track record
   (live_orders.json / proposals_log.json). Scheduled as a Monday routine.

Every function below still raises NotImplementedError: executor.py treats
`mode: "live"` as "propose + report that in-process live is not wired". The
real placements happen through path 3 and are visible in live_orders.json.

Stdlib-only, like the rest of the repo.
"""

import os

ENDPOINT = os.environ.get(
    "ROBINHOOD_MCP_ENDPOINT", "https://agent.robinhood.com/mcp/trading"
).strip()

# The connect command we print in error messages, so the path to fixing a
# not-wired live run is always one copy-paste away.
CONNECT_CMD = (
    f"claude mcp add robinhood-trading --transport http {ENDPOINT}"
)


class LiveTradingNotWired(NotImplementedError):
    """Raised when executor.py asks for a live action the adapter can't perform
    yet. executor.py catches this and degrades to a clearly-labelled proposal
    instead of crashing or, worse, silently doing nothing while claiming success.
    """


def _not_wired(action):
    raise LiveTradingNotWired(
        f"Robinhood MCP live action '{action}' is not wired.\n"
        f"  Endpoint: {ENDPOINT}\n"
        f"  This repo's executor is a proposal engine by design — see the module\n"
        f"  docstring in robinhood_mcp.py ('WHY THIS MODULE IS A STUB').\n"
        f"  To drive orders interactively, connect the MCP to your agent:\n"
        f"    {CONNECT_CMD}\n"
        f"  Programmatic (headless) order placement requires Agentic Trading\n"
        f"  access + an OAuth token + an MCP client, none of which ship here."
    )


def is_wired():
    """True only when a real programmatic implementation has been added below.
    executor.py calls this before attempting any live action."""
    return False


def get_account():
    """Return {"buying_power_usd": float, "account_value_usd": float, ...} for the
    dedicated Agentic account. Stub until path 2 is implemented."""
    _not_wired("get_account")


def get_positions():
    """Return [{"ticker": str, "quantity": float, "market_value_usd": float}, ...]
    for the Agentic account. Stub until path 2 is implemented."""
    _not_wired("get_positions")


def place_order(ticker, side, notional_usd=None, quantity=None,
                order_type="market", limit_price=None):
    """Place an order in the Agentic account. Stub until path 2 is implemented.

    Args mirror what the Robinhood MCP exposes (fractional notional or share
    quantity; market or limit). executor.py builds these from a vetted proposal.
    """
    _not_wired("place_order")
