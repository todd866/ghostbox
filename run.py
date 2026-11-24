#!/usr/bin/env python3
"""ghostbox CLI:  python3 run.py --goal-file examples/pizza_form.py
or one-off:      python3 run.py --goal "..." --guide "..." """
import argparse
import importlib.util
import os
import sys

from ghosthands import Agent, Config, Grounder, Planner, make_box, make_hands


def load_task(path):
    spec = importlib.util.spec_from_file_location("gh_task", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GOAL, mod.GUIDE, getattr(mod, "MAX_STEPS", 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal-file")
    ap.add_argument("--goal")
    ap.add_argument("--guide", default="")
    ap.add_argument("--run-dir", default=os.path.join(Config.runs_dir, "run"))
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--hands", default=None, help="pico | dryrun | cliclick (when --target mac)")
    ap.add_argument("--target", default=None, help="box (ghostbox container or GhostMirror, default) | mac")
    ap.add_argument("--box-url", default=None, help="ghostbox/GhostMirror control URL (default: GH_BOX_URL or http://127.0.0.1:8780)")
    a = ap.parse_args()
    if a.goal_file:
        goal, guide, max_steps = load_task(a.goal_file)
    elif a.goal:
        goal, guide, max_steps = a.goal, a.guide, a.max_steps
    else:
        print("need --goal-file or --goal")
        sys.exit(2)
    if not Config.api_key:
        print("no OPENROUTER_API_KEY or GH_API_KEY (env or ~/.config/ghosthands/openrouter.env)")
        sys.exit(2)
    target = a.target or os.environ.get("GH_TARGET") or "box"
    if target == "box":
        eyes, hands = make_box(a.box_url)
        hands_name = "box:%s" % hands.box.url
    elif target == "mac":
        eyes, hands = None, make_hands(a.hands)
        hands_name = a.hands or Config.hands_backend
    else:
        print("unknown --target %r (box | mac)" % target)
        sys.exit(2)
    agent = Agent(Planner(), Grounder(), hands, eyes=eyes, run_dir=a.run_dir)
    print("planner=%s grounder=%s hands=%s run_dir=%s" % (
        Config.planner_model, Config.grounder_model, hands_name, a.run_dir))
    result = agent.run(goal, guide, max_steps=max_steps)
    print("RESULT:", result)


if __name__ == "__main__":
    main()
