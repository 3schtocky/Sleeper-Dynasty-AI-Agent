"""Local model bake-off for Phase 5: routing accuracy, grounded answers, and speed, identical for
every model. Run against a running Ollama:

    uv run python benchmarks/model_bench.py qwen3:4b-instruct gemma4:e2b

Prints one JSON line per model. Results from 2026-09-28 are recorded in PLANNING.md (Phase 5,
step 0) and in ~/Open Source Models/MODELS.md. A starting point for Phase 5 step 6's evaluation
set, not the final one: the tools here are stand-ins shaped like the real ones.
"""
import json, re, statistics, sys, time
import httpx

URL = "http://localhost:11434/api/chat"
SYSTEM = ("You are a dynasty fantasy football assistant for the user's Sleeper league. Always answer by calling "
          "one of the tools; never guess numbers. The current week, season, and the user's roster are known to the "
          "tools, so don't ask for them. If the question isn't about fantasy football, reply briefly without a tool.")

def fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}

S = {"type": "string"}; L = {"type": "array", "items": {"type": "string"}}
TOOLS = [
    fn("evaluate_trade", "Evaluate a proposed trade: players and draft picks the user would send and receive.",
       {"send_players": L, "receive_players": L, "send_picks": {**L, "description": "e.g. '2027 1st'"}, "receive_picks": {**L, "description": "e.g. '2027 1st'"}}),
    fn("set_lineup", "The user's best starting lineup for this week against their real opponent."),
    fn("my_team", "The user's roster with each player's value, and whether to contend or rebuild."),
    fn("waiver_targets", "The best available free agents and suggested FAAB bids."),
    fn("faab_bid", "A suggested FAAB bid for one specific free agent.", {"player": S}, ["player"]),
    fn("player_outlook", "Value, recent production, and injury status for one player.", {"player": S}, ["player"]),
    fn("rookie_board", "Ranked rookie prospects for a draft class.", {"draft_class": {"type": "integer"}}),
    fn("pick_advice", "Buy, hold, or sell advice for the user's rookie draft picks."),
    fn("taxi_plan", "Which players to move to taxi or IR to free roster spots."),
    fn("game_conditions", "Weather and Vegas line for one player's game this week.", {"player": S}, ["player"]),
]

def has(args, key, *needles):
    blob = json.dumps(args.get(key, "")).lower()
    return all(n.lower() in blob for n in needles)

CASES = [
    ("Who should I start this week?", "set_lineup", lambda a: True),
    ("Set my lineup", "set_lineup", lambda a: True),
    ("Should I trade Jonah Coleman and my 2028 2nd for a 2027 1st?", "evaluate_trade",
     lambda a: has(a, "send_players", "coleman") and has(a, "send_picks", "2028") and has(a, "receive_picks", "2027")),
    ("Would you give CeeDee Lamb for Puka Nacua and Jaxon Smith-Njigba?", "evaluate_trade",
     lambda a: has(a, "send_players", "lamb") and has(a, "receive_players", "nacua", "smith-njigba")),
    ("I'm offered Bijan Robinson for Rashee Rice and my 2027 first. Take it?", "evaluate_trade",
     lambda a: has(a, "receive_players", "bijan") and has(a, "send_players", "rice") and has(a, "send_picks", "2027")),
    ("How much should I bid on Justin Fields?", "faab_bid", lambda a: has(a, "player", "fields")),
    ("Who's worth picking up on waivers?", "waiver_targets", lambda a: True),
    ("Am I a contender or should I rebuild?", "my_team", lambda a: True),
    ("How's my team looking?", "my_team", lambda a: True),
    ("Is Saquon still worth holding?", "player_outlook", lambda a: has(a, "player", "saquon")),
    ("Who are the best rookies in the 2027 class?", "rookie_board", lambda a: a.get("draft_class") in (2027, "2027")),
    ("Should I sell my first round pick?", "pick_advice", lambda a: True),
    ("Should I put anyone on my taxi squad?", "taxi_plan", lambda a: True),
    ("Is it going to be windy for Josh Allen's game?", "game_conditions", lambda a: has(a, "player", "allen")),
    ("What's the capital of France?", None, lambda a: True),
    ("thanks!", None, lambda a: True),
]

GROUNDING = [
    ({"trade": "send Jonah Coleman + 2028 2nd, receive 2027 1st", "market_value_sent": 3098, "market_value_received": 2839,
      "net_market_value": -259, "coleman_projected_ppg": 4.6, "posture": "unclear"},
     "Should I trade Jonah Coleman and my 2028 2nd for a 2027 1st?", ["3098", "2839", "259"],
     ("evaluate_trade", {"send_players": ["Jonah Coleman"], "send_picks": ["2028 2nd"], "receive_picks": ["2027 1st"]})),
    ({"recommended_lineup": ["Matthew Stafford QB 19.6", "Saquon Barkley RB 10.6", "Javonte Williams RB 13.7", "Rashee Rice WR 16.5"],
      "win_probability": 0.808, "bench_best": "Lamar Jackson QB 17.4"},
     "Who should I start this week?", ["80.8", "stafford"], ("set_lineup", {})),
    ({"pick": "2027 1.06", "fantasycalc_price": 3040, "comparable_rookie_value": 2268, "ratio": 1.34, "advice": "SELL",
      "slot_history_hit_rate": 0.39},
     "Should I sell my first round pick?", ["sell", "3040", "2268"], ("pick_advice", {})),
]

def chat(model, messages, tools=None, num_predict=256):
    t = time.time()
    body = {"model": model, "stream": False, "messages": messages, "think": False,
            "options": {"num_ctx": 8192, "temperature": 0, "num_predict": num_predict}}
    if tools: body["tools"] = tools
    r = httpx.post(URL, timeout=600, json=body)
    r.raise_for_status()
    return r.json(), time.time() - t

def numbers(text):
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}

def bench(model):
    chat(model, [{"role": "user", "content": "hi"}], num_predict=4)  # load into memory
    route_ok, route_times, fails = 0, [], []
    for q, want, check in CASES:
        d, secs = chat(model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}], TOOLS, 200)
        route_times.append(secs)
        calls = d["message"].get("tool_calls") or []
        got = calls[0]["function"]["name"] if calls else None
        args = calls[0]["function"]["arguments"] if calls else {}
        if isinstance(args, str):
            try: args = json.loads(args)
            except ValueError: args = {}
        ok = (got == want) and (want is None or check(args))
        route_ok += ok
        if not ok: fails.append(f"{q[:40]!r}: got {got} {json.dumps(args)[:80]}")
    ground_ok, invented = 0, 0
    for result, q, must, (tool_name, tool_args) in GROUNDING:
        msgs = [{"role": "system", "content": SYSTEM + " After a tool runs, explain its result to the user in 2 to 4 "
                 "plain sentences: give the recommendation, quote the key numbers exactly as the tool gave them, and "
                 "never add a number the tool didn't give."},
                {"role": "user", "content": q},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool_name, "arguments": tool_args}}]},
                {"role": "tool", "content": json.dumps(result), "tool_name": tool_name}]
        d, _ = chat(model, msgs, TOOLS, 250)
        text = (d["message"].get("content") or "").lower()
        ground_ok += all(m.lower() in text.replace(",", "") for m in must)
        allowed = numbers(json.dumps(result)) | {str(round(float(x) * 100, 1)).rstrip("0").rstrip(".") for x in re.findall(r"0\.\d+", json.dumps(result))} | {"1", "2", "3", "2027", "2028"}
        invented += len([n for n in numbers(text) if n not in allowed and n.rstrip("0").rstrip(".") not in allowed])
    gen = []
    for _ in range(2):
        d, _ = chat(model, [{"role": "user", "content": "Explain in about 250 words how dynasty rookie drafts work."}], None, 350)
        gen.append(d["eval_count"] / (d["eval_duration"] / 1e9))
    block = " ".join(f"Player{i} scored {i % 23}.{i % 7} points in week {i % 18 + 1}." for i in range(130))
    d, _ = chat(model, [{"role": "user", "content": f"{time.time()} {model} {block} Who scored most? One word."}], None, 8)
    prompt_rate = d["prompt_eval_count"] / (d["prompt_eval_duration"] / 1e9)
    return {"model": model, "route": f"{route_ok}/{len(CASES)}", "route_median_s": round(statistics.median(route_times), 2),
            "grounded": f"{ground_ok}/{len(GROUNDING)}", "invented_numbers": invented,
            "gen_tok_s": round(statistics.median(gen), 1), "prompt_tok_s": round(prompt_rate), "fails": fails}

if __name__ == "__main__":
    for m in sys.argv[1:]:
        try:
            print(json.dumps(bench(m)), flush=True)
        except Exception as e:
            print(json.dumps({"model": m, "error": f"{type(e).__name__}: {e}"}), flush=True)
