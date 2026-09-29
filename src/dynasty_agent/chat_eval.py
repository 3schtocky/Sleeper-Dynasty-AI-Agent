"""The chat's routing evaluation: real questions, the tool each should reach,
and what its arguments must say once the tool layer has normalized them.

Run live against the local model with `dynasty-agent chat-eval` (add
--record to save the model's raw answers to tests/data/recorded_routes.json);
tests/test_chat_routing.py replays that recording offline on every test run,
so a change to the tool schemas, the normalization or the prompt that would
misroute a question fails there first.

A trade is scored on direction, not just on reaching evaluate_trade: a model
that evaluates the reverse of the user's trade is the worst failure this
tool can have (it disqualified the faster models in the Phase 5 bake-off).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dynasty_agent import chat, picks, tools
from dynasty_agent.config import PROJECT_ROOT

RECORDING = PROJECT_ROOT / "tests" / "data" / "recorded_routes.json"
NEXT_DRAFT = 2027  # the recording's league; "next year's 1st" reads as this draft


# Whose roster each player in the cases is on in the recorded league, so the
# offline test applies tools.fix_sides the way the live tool does.
MY_PLAYERS = {"jonah coleman", "ceedee lamb", "rashee rice", "lamar jackson", "rome odunze"}
THEIR_PLAYERS = {"puka nacua", "jaxon smith-njigba", "bijan robinson", "jahmyr gibbs"}  # ambiguous names are never moved


def _fix_sides_offline(send, receive, send_specs, receive_specs):
    """tools.fix_sides without a database, from the ownership above."""
    def owner(name):
        n = name.lower()
        return "me" if any(m in n for m in MY_PLAYERS) else "them" if any(t in n for t in THEIR_PLAYERS) else None
    new_send = [n for n in send if owner(n) != "them"]
    new_receive = [n for n in receive if owner(n) != "me"]
    moved_in, moved_out = [n for n in send if owner(n) == "them"], [n for n in receive if owner(n) == "me"]
    new_send += moved_out
    new_receive += moved_in
    if moved_in or moved_out:
        if not new_send and not send_specs and receive_specs and new_receive:
            send_specs, receive_specs = receive_specs, []
        elif not new_receive and not receive_specs and send_specs and new_send:
            receive_specs, send_specs = send_specs, []
    return new_send, new_receive, send_specs, receive_specs


def normalized(call: dict | None) -> dict:
    """The arguments the tool layer would act on: name lists split out of
    strings, picks moved out of player lists, players the rosters place on
    the other side moved there (tools.fix_sides), picks read into (season,
    round)."""
    if not call:
        return {}
    args = call.get("arguments") or {}
    if call.get("name") != "evaluate_trade":
        return {k: tools._blank_if_none(v).lower() if isinstance(v, str) else v for k, v in args.items()}
    split = {side: tools._split_players_and_picks(tools._as_list(args.get(f"{side}_players")),
                                                  tools._as_list(args.get(f"{side}_picks")))
             for side in ("send", "receive")}
    send, receive, send_specs, receive_specs = _fix_sides_offline(split["send"][0], split["receive"][0],
                                                                  split["send"][1], split["receive"][1])
    out = {"send_players": [p.lower() for p in send], "receive_players": [p.lower() for p in receive]}
    for side, specs in (("send", send_specs), ("receive", receive_specs)):
        parsed = []
        for spec in specs:
            try:
                p = picks.parse_pick(spec, first_season=NEXT_DRAFT)
                parsed.append((p.season, p.round))
            except ValueError:
                parsed.append(("unreadable", spec))
        out[f"{side}_picks"] = parsed
    return out


def _has(values: list, *needles: str) -> bool:
    blob = " ".join(str(v) for v in values).lower()
    return all(n in blob for n in needles)


def trade(send: tuple = (), receive: tuple = (), send_picks: tuple = (), receive_picks: tuple = ()) -> Callable[[dict], bool]:
    """Passes when each named player and (season, round) pick sits on the side given."""
    def check(a: dict) -> bool:
        return (
            all(_has(a.get("send_players", []), n) for n in send)
            and all(_has(a.get("receive_players", []), n) for n in receive)
            and all(p in a.get("send_picks", []) for p in send_picks)
            and all(p in a.get("receive_picks", []) for p in receive_picks)
            and not any(_has(a.get("receive_players", []), n) for n in send)
            and not any(_has(a.get("send_players", []), n) for n in receive)
        )
    return check


def anything(a: dict) -> bool:
    return True


@dataclass
class Case:
    question: str
    tools: tuple  # acceptable tools; None means answer without a tool
    check: Callable[[dict], bool] = anything
    kind: str = "routing"


CASES = [
    # lineup
    Case("Who should I start this week?", ("set_lineup",)),
    Case("Set my lineup", ("set_lineup",)),
    Case("Who do I sit this week?", ("set_lineup",)),
    Case("Optimize my starters for Sunday", ("set_lineup",)),
    # trades, direction is the point
    Case("Should I trade Jonah Coleman and my 2028 2nd for a 2027 1st?", ("evaluate_trade",),
         trade(send=("coleman",), send_picks=((2028, 2),), receive_picks=((2027, 1),)), "trade direction"),
    Case("Would you give CeeDee Lamb for Puka Nacua and Jaxon Smith-Njigba?", ("evaluate_trade",),
         trade(send=("lamb",), receive=("nacua", "smith-njigba")), "trade direction"),
    Case("I'm offered Bijan Robinson for Rashee Rice and my 2027 first. Take it?", ("evaluate_trade",),
         trade(send=("rice",), receive=("bijan",), send_picks=((2027, 1),)), "trade direction"),
    Case("Someone wants my 2027 1st for their Puka Nacua. Good deal?", ("evaluate_trade",),
         trade(receive=("nacua",), send_picks=((2027, 1),)), "trade direction"),
    Case("Can I get Puka Nacua for CeeDee Lamb straight up?", ("evaluate_trade",),
         trade(send=("lamb",), receive=("nacua",)), "trade direction"),
    Case("Should I trade away Lamar Jackson for a 2028 second?", ("evaluate_trade",),
         trade(send=("lamar",), receive_picks=((2028, 2),)), "trade direction"),
    Case("A guy offered me his 2028 1st for Rome Odunze, should I accept?", ("evaluate_trade",),
         trade(send=("odunze",), receive_picks=((2028, 1),)), "trade direction"),
    # picks written every which way
    Case("Trade my '27 first for their 2028 1st?", ("evaluate_trade",),
         trade(send_picks=((2027, 1),), receive_picks=((2028, 1),)), "pick formats"),
    Case("Is 2027-1 for 2028-1 plus 2028-2 worth it for me?", ("evaluate_trade",),
         trade(send_picks=((2027, 1),), receive_picks=((2028, 1), (2028, 2))), "pick formats"),
    Case("Would you trade my 2027 round 1 pick for Trey Benson?", ("evaluate_trade",),
         trade(send_picks=((2027, 1),), receive=("benson",)), "pick formats"),
    Case("Should I flip my 2027 1st and 2027 2nd for Jahmyr Gibbs?", ("evaluate_trade",),
         trade(send_picks=((2027, 1), (2027, 2)), receive=("gibbs",)), "pick formats"),
    Case("Trade my 2029 third for a 2028 third?", ("evaluate_trade",),
         trade(send_picks=((2029, 3),), receive_picks=((2028, 3),)), "pick formats"),
    # names the league can't match: still a trade, the tool asks
    Case("Trade Tom Brady for Puka Nacua?", ("evaluate_trade",), trade(send=("brady",), receive=("nacua",)), "unknown player"),
    Case("Trade Kenneth Walker for Puka Nacua?", ("evaluate_trade",), trade(send=("walker",), receive=("nacua",)), "ambiguous player"),
    # team
    Case("Am I a contender or should I rebuild?", ("my_team",)),
    Case("How's my team looking?", ("my_team",)),
    Case("Am I a contender?", ("my_team",)),
    Case("Should I rebuild?", ("my_team",)),
    # waivers
    Case("Who's worth picking up on waivers?", ("waiver_targets",), lambda a: not a.get("position") and not a.get("player")),
    Case("Who should I pick up?", ("waiver_targets",), lambda a: not a.get("position") and not a.get("player")),
    Case("Any tight ends worth picking up?", ("waiver_targets",), lambda a: a.get("position") in ("te", "tight end")),
    Case("How much should I bid on Trey Benson?", ("waiver_targets",), lambda a: "benson" in (a.get("player") or "")),
    Case("What's a good FAAB bid for Justin Fields?", ("waiver_targets",), lambda a: "fields" in (a.get("player") or "")),
    # picks
    Case("Should I sell my first round pick?", ("pick_advice",)),
    Case("Should I sell my first?", ("pick_advice",)),
    Case("Are my draft picks worth more than the rookies they'd get?", ("pick_advice",)),
    # taxi and IR
    Case("Should I put anyone on my taxi squad?", ("taxi_plan",)),
    Case("Anyone I should put on taxi?", ("taxi_plan",)),
    Case("Who should go on IR?", ("taxi_plan",)),
    # not the league's business: no tool, and never a made-up stat
    Case("What's the capital of France?", (None,), kind="off topic"),
    Case("thanks!", (None,), kind="off topic"),
    Case("Write me a short poem about autumn", (None,), kind="off topic"),
    Case("Who won the Super Bowl in 1990?", (None,), kind="off topic"),
    # asked to guess: a tool (whose numbers are real) or no tool, never a number from memory
    Case("Don't look anything up, just guess how many points CeeDee Lamb scores this week", (None, "set_lineup", "my_team"),
         kind="asked to invent"),
    Case("Roughly what's Puka Nacua's trade value? Just ballpark it", (None, "evaluate_trade", "my_team"), kind="asked to invent"),
]


def first_call(calls: list[dict]) -> dict | None:
    return calls[0] if calls else None


def score(case: Case, call: dict | None) -> tuple[bool, str]:
    got = call["name"] if call else None
    if got not in case.tools:
        return False, f"routed to {got}, wanted {' or '.join(str(t) for t in case.tools)}"
    if got is not None and not case.check(normalized(call)):
        return False, f"{got} with wrong arguments: {json.dumps(normalized(call))}"
    return True, ""


def run_live(client, record: bool = False) -> dict:
    """Route every case through the real model with the chat's own prompt and
    tools; score it; optionally save the raw answers for the offline test."""
    recorded, results, started = {}, [], time.monotonic()
    for case in CASES:
        messages = [{"role": "system", "content": chat.ROUTE_SYSTEM}, {"role": "user", "content": case.question}]
        calls, text, stats = client.route(messages, tools.TOOLS)
        call = first_call(calls)
        ok, why = score(case, call)
        recorded[case.question] = {"calls": calls, "text": text}
        results.append({"question": case.question, "kind": case.kind, "ok": ok, "why": why,
                        "seconds": round(stats.total_s, 2)})
    if record:
        RECORDING.parent.mkdir(parents=True, exist_ok=True)
        RECORDING.write_text(json.dumps({"model": client.model, "recorded": time.strftime("%Y-%m-%d"),
                                         "routes": recorded}, indent=1) + "\n", encoding="utf-8")
    return {"model": client.model, "passed": sum(r["ok"] for r in results), "total": len(results),
            "seconds": round(time.monotonic() - started, 1), "results": results}


def load_recording(path: Path = RECORDING) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
