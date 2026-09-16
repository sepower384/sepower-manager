# -*- coding: utf-8 -*-
"""cron-job.org 에 세력의 매니저 트리거 2개 등록 (같은 제목은 지우고 다시 만듦 = 여러 번 돌려도 안전).

  python tools/cron_setup.py <GitHub PAT(Actions RW, sepower-manager 포함)> <cron-job.org API 키>

  [GH] manager-updates : 5분마다 (07~24시 + 00시대)  mode=updates
  [GH] manager-cycle   : 30분마다 (07~23시, 03·33분)   mode=cycle
"""
import json
import sys

import requests

REPO = "sepower384/sepower-manager"
API = "https://api.cron-job.org"
DISPATCH = "https://api.github.com/repos/%s/actions/workflows/manager.yml/dispatches" % REPO


def job(title, mode, minutes, hours, pat):
    return {"job": {
        "url": DISPATCH, "enabled": True, "title": title, "saveResponses": False,
        "requestMethod": 1,  # POST
        "schedule": {"timezone": "Asia/Seoul", "expiresAt": 0, "hours": hours,
                     "mdays": [-1], "minutes": minutes, "months": [-1], "wdays": [-1]},
        "notification": {"onFailure": True, "onSuccess": False, "onDisable": True},
        "extendedData": {
            "headers": {"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
            "body": json.dumps({"ref": "main", "inputs": {"mode": mode}})},
    }}


def main(pat, key):
    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    # PAT 권한 확인: 없는 ref 로 dispatch → 422 면 권한 있음
    r = requests.post(DISPATCH, json={"ref": "no-such-ref"},
                      headers={"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json"})
    if r.status_code != 422:
        sys.exit("PAT 권한 없음(%d): %s — PAT 의 Repository access 에 sepower-manager 추가, Actions Read/write" % (r.status_code, r.text[:200]))
    specs = [
        ("[GH] manager-updates", "updates", list(range(0, 60, 5)), list(range(7, 24)) + [0]),
        ("[GH] manager-cycle", "cycle", [3, 33], list(range(7, 24))),
    ]
    jobs = requests.get(API + "/jobs", headers=h).json().get("jobs", [])
    titles = {s[0] for s in specs}
    for j in jobs:
        if j["title"] in titles:
            requests.delete(API + "/jobs/%d" % j["jobId"], headers=h).raise_for_status()
            print("삭제:", j["title"])
    for title, mode, minutes, hours in specs:
        r = requests.put(API + "/jobs", headers=h, data=json.dumps(job(title, mode, minutes, hours, pat)))
        r.raise_for_status()
        print("등록:", title, r.json())


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
