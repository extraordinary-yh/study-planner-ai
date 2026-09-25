"""Create a complete fictional day without altering a personal workspace."""
from pathlib import Path
from datetime import date
import argparse
import os
import shutil
from studyplan.plan import Plan, Block, save_plan, quality_issues
from studyplan.board import plan_payload, render_html, serve
root=Path(__file__).resolve().parents[1]
workspace=root/"build/demo"
shutil.copytree(root/"examples/profile",workspace/"profile",dirs_exist_ok=True)
shutil.copytree(root/"config",workspace/"config",dirs_exist_ok=True)
os.environ["STUDYPLAN_ROOT"]=str(workspace)
os.environ.pop("JOBRADAR_ROOT",None)
today=date.today().isoformat()
blocks=[]
for id,title,start,minutes,url,purpose,items in [
 ("leetcode","Coding · Two Sum","10:00",30,"https://leetcode.com/problems/two-sum/","Practice a hash-map invariant.",[("Solve the problem without a saved solution",20),("Test duplicates and explain the invariant",10)]),
 ("design","System design · Request lifecycle","10:45",30,"https://developer.mozilla.org/en-US/docs/Web/HTTP/Overview","Trace an HTTP request and identify a failure boundary.",[("Read the HTTP overview",15),("Sketch one request and explain a timeout",15)]),
 ("stories","Behavioral · A technical tradeoff","11:30",20,"https://example.com","Organize one true experience; the demo contains no career claims.",[("Choose your own example and outline the decision",10),("Explain the tradeoff and list missing evidence",10)]),
]:
    b=Block(id=id,title=title,start=start,minutes=minutes,first_action=f"open [Required today]({url}) and read the first screen.",description=["Purpose: "+purpose],links=[{"label":"Required today","url":url}])
    for label,cap in items:b.add_item(f"{label} ({cap} min)",refs=[url])
    blocks.append(b)
plan=Plan(date=today,minutes=80,radar=["FICTIONAL DEMO · no real learner history or calendar"],blocks=blocks,call="Start with one coding problem, then explain how a request travels through a service.")
issues=quality_issues(plan)
if issues: raise RuntimeError(issues)
save_plan(workspace,plan)
preview=workspace/"preview.html"
preview.write_text(render_html(plan_payload(workspace,today)))
print(f"Fictional demo: {preview}")
print("Interactive board: uv run python scripts/demo.py --serve")
if argparse.ArgumentParser().parse_known_args()[1] == ["--serve"]:
    serve(workspace,today,port=8765,host="127.0.0.1")
