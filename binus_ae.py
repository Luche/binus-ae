"""Automates BINUS Activity Enrichment supervisor work and writes a progress tracker page.

Site Supervisor: approves logbooks. Faculty Supervisor: approves logbooks, approves/extends assignments and
approves final reports of students who finished everything. Run `binus-ae setup` once; see README.md.
"""

import argparse
import getpass
import html
import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import keyring
from dotenv import load_dotenv
from keyring.errors import KeyringError
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://activity-enrichment.apps.binus.ac.id"
HOME = Path(os.getenv("BINUS_AE_HOME", "~/.binus-ae")).expanduser()
CONFIG = HOME / "config.env"
PROFILE = HOME / "profile-fs"
PROFILE_SS = HOME / "profile-ss"
LOGS = HOME / "logs"
STATE = HOME / "last_success"  # month of the last full successful run; the reminder stays quiet for that month
KEYRING = "binus-ae"
REMINDER_LABEL = "com.binus-ae.reminder"
REMINDER = Path.home() / "Library/LaunchAgents" / f"{REMINDER_LABEL}.plist"

# (sidebar link or page path, period <select> id)
LOGBOOK = ("Logbook Activity", "#strmLogbook")
ASSIGNMENT = ("Assignment", "#strmAssignment")
FINAL_REPORT = ("/FinalReport/FinalReportFSIndex", "#strmFinalReportFS")

log = logging.getLogger("binus_ae")

JS_SETTLED = r"""() => {
  if (window.jQuery && jQuery.active > 0) return false;
  const busy = document.querySelector('.fancybox-opened, .fancybox-wrap, .dataTables_processing');
  return !(busy && busy.offsetParent !== null && /loading|processing/i.test(busy.innerText || ''));
}"""

# Student list rows; yellow = first cell has background rgb(255, 255, 0).
# First cell is a BINUSIAN ID, or a final report group id like 2520IN-AbCd1234.
JS_STUDENT_ROWS = r"""() => [...document.querySelectorAll('tr')].flatMap(tr => {
  const tds = tr.querySelectorAll(':scope > td');
  if (tr.offsetParent === null || tds.length < 3) return [];
  const id = tds[0].innerText.trim();
  if (!/^(BN\d+|\d{4}[A-Z]+-\w+)$/i.test(id)) return [];
  const m = getComputedStyle(tds[0]).backgroundColor.match(/[\d.]+/g) || [];
  const [r, g, b] = m.map(Number);
  const a = tr.querySelector('a');
  return [{id, name: (a && a.innerText.trim()) || tds[2].innerText.trim(), yellow: r >= 200 && g >= 200 && b <= 150,
           status: tds[tds.length - 1].innerText.trim()}];
})"""

# Month tabs: <ul id="monthTab"><li><a>SEPTEMBER <span style="color:#d12f2e">●</span></a></li>
JS_MONTH_TABS = r"""() => [...document.querySelectorAll('#monthTab a')].map((a, i) => {
  a.setAttribute('data-ae-tab', i);
  return {i, name: a.innerText.replace(/[●•]/g, '').trim(), dot: /[●•]/.test(a.innerText)};
})"""

# Assignment rows: .approveBtn (status 5/6) or ExtendDueDate(...) button (status 7); hidden on inactive terms.
JS_ASSIGNMENT_ROWS = r"""() => [...document.querySelectorAll('#assignmentTable tbody tr')].flatMap((tr, i) => {
  const approve = tr.querySelector('.approveBtn');
  const extend = tr.querySelector('button[onclick^="ExtendDueDate"]');
  const btn = [approve, extend].find(b => b && b.offsetParent !== null);
  if (!btn) return [];
  btn.setAttribute('data-ae-btn', i);
  const key = [...tr.querySelectorAll(':scope > td')].slice(2).map(td => td.innerText.trim()).join(' | ');
  return [{i, key, action: btn === approve ? 'approve' : 'extend'}];
})"""

JS_LIST_READY = r"""() => !!document.querySelector('.dataTables_empty') ||
  [...document.querySelectorAll('tr > td:first-child')].some(td => /^(BN\d+|\d{4}[A-Z]+-\w+)$/i.test(td.innerText.trim()))"""

JS_SET_VALUE = "(el, v) => { jQuery(el).val(v).trigger('change'); }"

JS_TABLE_ROWS = "() => jQuery('#StudentListTable').DataTable().rows().data().toArray()"

# Per student: logbook days of every month and all assignments, straight from the site's JSON endpoints.
JS_COLLECT = r"""async ([students, strm]) => {
  const call = (url, type, data) => jQuery.ajax({url, type, data}).then(r => r, () => null);
  const text = h => (h || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  const one = async s => {
    const m = await call('/LogBook/GetMonths', 'GET', {logBookId: '', binusianID: s.emplId, tipe: 2, strm});
    const months = await Promise.all(((m && m.data) || []).map(async mo => {
      if (!mo.logBookHeaderID || /^0{8}-/.test(mo.logBookHeaderID)) return {month: mo.month, days: []};
      const r = await call('/LogBook/GetLogBook', 'POST', {logBookHeaderID: mo.logBookHeaderID, binusianID: s.emplId, strm, trackId: s.trackId});
      return {month: mo.month, err: !r, days: ((r && r.data) || []).map(d => [d.acceptanceID, text(d.acceptance), (d.date || '').slice(0, 7)])};
    }));
    const a = await call('/Assignment/GetStudentAssignment', 'POST', {BinusianId: s.emplId, TrackId: s.trackId, Month: 0, strm});
    const asg = ((a && a.data) || []).map(x => ({month: x.assignmentMonthDesc, sid: String(x.assignmentStatusId ?? ''),
                                               status: x.assignmentStatusName || '', due: x.assignmentDueDate}));
    return {id: s.emplId, err: !m || !a, months, asg};
  };
  const out = [];
  for (let i = 0; i < students.length; i += 4) out.push(...await Promise.all(students.slice(i, i + 4).map(one)));
  return out;
}"""


class Bot:
    def __init__(self, page, args, ss=False):
        self.page = page
        self.ss = ss
        self.dry = args.dry_run
        self.limit = args.limit
        self.feedback = os.getenv("FEEDBACK", "ok")
        self.dialogs: list[str] = []
        self.status = None
        self.stats = {"logbook_months": 0, "logbook_students": 0, "asg_approved": 0, "asg_extended": 0, "fr_groups": 0, "errors": 0}
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, d):
        log.info("  popup: %s", d.message.strip())
        self.dialogs.append(d.message)
        try:
            d.accept()
        except PWError:
            pass

    def settle(self, timeout=45000):
        p = self.page
        p.wait_for_timeout(500)
        for _ in range(3):
            try:
                p.wait_for_function(JS_SETTLED, timeout=timeout, polling=250)
                break
            except PWTimeout:
                log.warning("  page did not settle in time")
                break
            except PWError:  # navigation in progress
                p.wait_for_timeout(1000)
        p.wait_for_timeout(300)

    def wait_popup(self, before, timeout=30):
        end = time.time() + timeout
        while time.time() < end and len(self.dialogs) == before:
            self.page.wait_for_timeout(250)
        self.settle()
        return self.dialogs[before:]

    # ---------- student list ----------

    def goto_menu(self, menu):
        link, sel = menu
        p = self.page
        if p.locator(sel).count():
            return
        if link.startswith("/"):
            p.goto(BASE + link, wait_until="load")
        else:
            p.get_by_role("link", name=re.compile(rf"^\s*{re.escape(link)}\s*$", re.I)).first.click()
        p.locator(sel).wait_for(state="attached")
        p.wait_for_load_state("load")
        self.settle()
        p.wait_for_function(JS_LIST_READY)

    def open_list(self, menu, period):
        p = self.page
        self.goto_menu(menu)
        if p.locator(menu[1]).input_value() != period["v"]:
            p.locator(menu[1]).evaluate(JS_SET_VALUE, period["v"])
            self.settle()
            p.wait_for_function(JS_LIST_READY)
        length = p.locator("select[name$='_length']").first
        if length.count() and length.input_value() != "100":
            length.evaluate(JS_SET_VALUE, "100")
            self.settle()
        first = p.locator(".dataTables_paginate a.paginate_button:not(.previous):not(.next)").first
        if first.count() and "current" not in (first.get_attribute("class") or ""):
            first.click()
            self.settle()

    def periods(self, menu, picks):
        opts = self.page.locator(f"{menu[1]} option").evaluate_all(
            "os => os.map(o => ({v: o.value, t: o.textContent.trim()}))"
        )
        opts = [o for o in opts if o["v"]]
        return [opts[i - 1] for i in sorted(set(picks)) if 0 < i <= len(opts)]

    def next_page(self):
        nxt = self.page.locator(".dataTables_paginate a.paginate_button.next").first
        if not nxt.count() or "disabled" in (nxt.get_attribute("class") or ""):
            return False
        nxt.click()
        self.settle()
        return True

    def next_yellow(self, menu, period, done):
        self.open_list(menu, period)
        while True:
            for r in self.page.evaluate(JS_STUDENT_ROWS):
                # Assignment: neither the yellow highlight nor STATUS reliably shows extension requests, so check everyone.
                # Site Supervisor: yellow means already approved by SS, so check everyone for red-dotted months.
                if (menu is ASSIGNMENT or self.ss or r["yellow"]) and r["id"] not in done:
                    return r
            if not self.next_page():
                return None

    def open_student(self, menu, period, r):
        self.open_list(menu, period)
        while True:
            link = self.page.locator(f"xpath=//tr[td[normalize-space(.)='{r['id']}']]//a").first
            if link.count():
                if menu is FINAL_REPORT:
                    self.page.evaluate("() => jQuery('#approval').hide()")
                    link.click()
                    self.page.locator("#approval").wait_for(state="visible")
                else:
                    link.click()
                    self.page.wait_for_function(
                        "n => (document.getElementById('StudentName')?.textContent || '').includes(n)", arg=r["name"]
                    )
                self.settle()
                return True
            if not self.next_page():
                log.warning("  student row not found anymore")
                return False

    def each_yellow(self, menu, periods, handler):
        handled = 0
        self.goto_menu(menu)
        for period in self.periods(menu, periods):
            log.info("Period: %s", period["t"])
            done = set()
            while self.limit is None or handled < self.limit:
                r = self.next_yellow(menu, period, done)
                if not r:
                    break
                done.add(r["id"])
                handled += 1
                log.info(" Student %s %s", r["id"], r["name"])
                try:
                    if self.open_student(menu, period, r):
                        handler(r)
                except Exception as e:
                    self.stats["errors"] += 1
                    log.exception("  ERROR on %s %s: %s", r["id"], r["name"], e)

    # ---------- logbook ----------

    def run_logbook(self, periods):
        log.info("=== LOGBOOK ===")
        self.each_yellow(LOGBOOK, periods, self.logbook_student)

    def logbook_student(self, r):
        attempted = set()
        approved = False
        while True:
            pending = [t for t in self.page.evaluate(JS_MONTH_TABS) if t["dot"] and t["name"] not in attempted]
            if not pending:
                if not attempted:
                    log.info("  no red-dotted month")
                break
            tab = pending[0]
            attempted.add(tab["name"])
            self.page.locator(f"[data-ae-tab='{tab['i']}']").click()
            self.settle()
            approved |= self.logbook_month(tab["name"])
        if approved:
            self.stats["logbook_students"] += 1

    def logbook_month(self, month):
        p = self.page
        selects = p.locator("#logBookTable select[id^='select_'], #logBookTable select[class*='select_']")  # SS selects have only the class
        n = selects.count()
        approve = "3" if self.ss else "4"  # Approve value: 3 for Site Supervisor, 4 for Faculty Supervisor
        for i in range(n):
            if selects.nth(i).input_value() != approve:
                selects.nth(i).evaluate(JS_SET_VALUE, approve)
        submit = p.locator("#btnsbt")
        if not submit.is_visible():
            log.warning("  %s: no SUBMIT button (nothing to approve?)", month)
            return False
        now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
        if self.dry:
            extra = "" if self.ss else f", date={now}, feedback={self.feedback!r}"
            log.info("  [dry-run] %s: would approve %d day(s)%s", month, n, extra)
            return False
        if not self.ss:  # Site Supervisor has no consultation date/feedback
            p.locator("#ConsDate").evaluate(JS_SET_VALUE, now)
            p.locator("#Feedback").fill(self.feedback)
            p.locator("#Feedback").evaluate("el => jQuery(el).trigger('change')")
        before = len(self.dialogs)
        submit.click()
        msgs = self.wait_popup(before)
        if any(re.search(r"success", m, re.I) for m in msgs):
            self.stats["logbook_months"] += 1
            log.info("  %s: approved %d day(s)", month, n)
            return True
        self.stats["errors"] += 1
        log.warning("  %s: submit did not report success (%s)", month, msgs)
        return False

    # ---------- assignment ----------

    def run_assignment(self, periods):
        log.info("=== ASSIGNMENT ===")
        self.each_yellow(ASSIGNMENT, periods, self.assignment_student)

    def assignment_student(self, r):
        attempted = set()
        while True:
            rows = [x for x in self.page.evaluate(JS_ASSIGNMENT_ROWS) if x["key"] not in attempted]
            if not rows:
                if not attempted:
                    log.info("  no Approve/Extend buttons")
                return
            row = rows[0]
            attempted.add(row["key"])
            if self.dry:
                log.info("  [dry-run] would %s: %s", row["action"], row["key"])
                continue
            before = len(self.dialogs)
            self.page.locator(f"[data-ae-btn='{row['i']}']").click()
            # Approve shows no popup; Extend shows one.
            msgs = self.wait_popup(before, timeout=20 if row["action"] == "extend" else 3)
            self.stats["asg_approved" if row["action"] == "approve" else "asg_extended"] += 1
            log.info("  %s: %s %s", row["action"], row["key"], msgs or "")

    # ---------- final report ----------

    def run_final_report(self, periods):
        log.info("=== FINAL REPORT ===")
        self.each_yellow(FINAL_REPORT, periods, self.final_report_group)

    def final_report_group(self, r):
        p = self.page
        selects = p.locator("#detailsTable select[id^='select_']")
        unviewed = p.locator("#detailsTable label", has_text=re.compile("PLEASE VIEW THE FILE", re.I))
        n = selects.count() + unviewed.count()
        if not n:
            log.info("  nothing to approve")
            return
        unfinished = self.unfinished_members(r["id"])
        if unfinished:
            log.info("  skip: not finished (%s)", "; ".join(unfinished))
            return
        submit = p.locator("#btnsbt")
        if not submit.is_visible():
            log.warning("  no SUBMIT button (inactive term?)")
            return
        if self.dry:
            log.info("  [dry-run] would download all files and approve %d file(s)", n)
            return
        if unviewed.count():
            # Files must be viewed before the Approve selects appear; "Download All File" marks them viewed.
            try:
                with p.context.expect_page(timeout=15000) as tab:
                    p.locator("#downloadBtn2").click()
                tab.value.close()
            except PWTimeout:
                pass
            self.settle()
        p.locator("#selectapproval").evaluate(JS_SET_VALUE, "3")  # 3 = Approve all
        if not selects.count() or not submit.is_enabled():
            self.stats["errors"] += 1
            log.warning("  could not enable SUBMIT (%d file(s) still unviewed)", unviewed.count())
            return
        n = selects.count()
        before = len(self.dialogs)
        submit.click()
        end = time.time() + 60
        while time.time() < end:
            if p.locator("#btnok:visible").count():
                log.info("  approved %d file(s): %s", n, p.locator("#allApprovedPopUp h3").inner_text().strip())
                self.stats["fr_groups"] += 1
                p.locator("#btnok:visible").click()
                self.settle()
                return
            if p.locator("#popupAssess:visible").count():
                self.stats["errors"] += 1
                log.warning("  site asks for assessment scores here; not automated, skipped")
                p.reload(wait_until="load")
                self.settle()
                return
            if len(self.dialogs) > before:
                self.settle()
                if any(re.search(r"success|approved", m, re.I) for m in self.dialogs[before:]):
                    self.stats["fr_groups"] += 1
                else:
                    self.stats["errors"] += 1
                return
            p.wait_for_timeout(250)
        self.stats["errors"] += 1
        log.warning("  no confirmation after SUBMIT")

    def unfinished_members(self, group_id):
        """Why the group's students aren't finished yet; empty when every member is complete."""
        for per in self.status or []:
            g = per["groups"].get(group_id)
            if not g:
                continue
            names = [x.strip() for x in re.split(r",|<br\s*/?>|\n", g["memberDetails"] or "") if x.strip()]
            members = [s for s in per["students"] if s["id"] == g["binusianID"] or s["name"] in names]
            if not members or len(members) < len(names):
                return ["student not found in logbook list"]
            return [f"{s['name']}: {', '.join(t for _, t in s['todo']) or 'not complete'}" for s in members if not s["complete"]]
        return ["no status for this group"]

    # ---------- status / tracker ----------

    def table_rows(self, menu, period):
        self.open_list(menu, period)
        rows = []
        while True:
            rows += self.page.evaluate(JS_TABLE_ROWS)
            if not self.next_page():
                return rows

    def collect_status(self, periods):
        log.info("=== STATUS ===")
        self.goto_menu(LOGBOOK)
        self.status = []
        for period in self.periods(LOGBOOK, periods):
            students = self.table_rows(LOGBOOK, period)
            raw = self.page.evaluate(JS_COLLECT, [[{"emplId": s["emplId"], "trackId": s["trackId"]} for s in students], period["v"]])
            per = build_period(period, students, raw)
            self.status.append(per)
            if any(r["err"] or any(m.get("err") for m in r["months"]) for r in raw):
                log.warning("  some status requests failed in %s", period["t"])
        self.collect_groups(periods)
        for per in self.status:
            st = per["students"]
            log.info("Status %s: %d students, %d complete, %d need you", per["t"], len(st),
                     sum(s["complete"] for s in st), sum(s["needs_me"] for s in st))

    def collect_groups(self, periods):
        self.goto_menu(FINAL_REPORT)
        for period in self.periods(FINAL_REPORT, periods):
            per = next((x for x in self.status if x["v"] == period["v"]), None)
            if per is None:
                continue
            per["groups"] = {g["groupUniqueId"]: g for g in self.table_rows(FINAL_REPORT, period)}
            by_id = {g["binusianID"]: g for g in per["groups"].values()}
            for s in per["students"]:
                set_final_report(s, by_id.get(s["id"]))


# ---------- status ----------

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
PRIORITY = ["me", "ss", "student", "progress", "future", "ok", "none"]  # first = most urgent


def month_years(raw):
    """Month name -> (year, month), from logbook day dates; months without days are placed next to known ones."""
    known = {}
    for r in raw:
        for mo in r["months"]:
            d = next((d[2] for d in mo["days"] if d[2]), None)
            if d:
                known.setdefault(mo["month"], tuple(map(int, d.split("-"))))
    if not known:
        return {}
    y0, m0 = next(iter(known.values()))
    for name in MONTHS:
        if name not in known:
            mi = MONTHS.index(name) + 1
            known[name] = min(((y, mi) for y in (y0 - 1, y0, y0 + 1)), key=lambda ym: abs(ym[0] * 12 + ym[1] - y0 * 12 - m0))
    return known


def logbook_cell(days, ym, today):
    ids = [d[0] for d in days]
    if ym and ym > today:
        return ("future", "⬜", "")
    if 3 in ids:
        return ("me", "⏳ FS", f"{ids.count(3)}d")
    if 1 in ids:
        return ("ss", "⏳ SS", "")
    if any(i in (2, 5) or "reject" in t.lower() for i, t, _ in days):
        return ("student", "✏️", "rejected")
    if ym == today:
        return ("progress", "📝", "")
    if not days:
        return ("student", "❌", "empty")
    if all(i == 4 for i in ids):
        return ("ok", "✅", "")
    if -1 not in ids and 0 in ids:
        return ("student", "❌", "not sent")
    missing = sum(i in (0, -1) for i in ids)
    if missing:
        return ("student", "❌", f"{missing}d missing")
    return ("student", "?", next(t for i, t, _ in days if i != 4)[:20])


def assignment_kind(a, now):
    sid, name = a["sid"], a["status"].lower()
    if sid == "3" or "approved by faculty" in name:
        return ("ok", "✅")
    if sid == "7" or "proposed extend" in name:
        return ("me", "⏳ ext")
    if sid in ("5", "6") or "proposed" in name or "submi" in name:
        return ("me", "⏳ FS")
    if "reject" in name:
        return ("student", "✏️")
    if sid == "2" or "extended" in name:
        return ("student", "🕒")
    if sid == "1" or "expired" in name:
        return ("student", "❌")
    if not name:
        due = datetime.fromisoformat(a["due"]) if a["due"] else None
        return ("future", "⬜") if due and due > now else ("student", "❌")
    return ("student", a["status"][:15])


def assignment_cell(rows, now):
    kinds = [assignment_kind(a, now) for a in rows]
    kind, label = min(kinds, key=lambda k: PRIORITY.index(k[0]))
    ok = sum(k[0] == "ok" for k in kinds)
    return (kind, label, f"{ok}/{len(kinds)}" if len(kinds) > 1 and kind in ("me", "ss", "student") else "")


def set_final_report(s, g):
    t = ((g or {}).get("groupStatus") or "").lower()
    if not g:
        cell = ("none", "—", "")
    elif g["groupStatusId"] == 3 or "approved by faculty" in t:
        cell = ("ok", "✅", "")
    elif "site" in t:
        cell = ("ss", "⏳ SS", "")
    elif g["groupStatusId"] == 2 or "faculty" in t:
        cell = ("me", "⏳ FS", "")
    else:
        cell = ("student", "❌", g["groupStatus"][:20])
    s["fr"] = cell
    s["todo"] = [x for x in s["todo"] if not x[1].startswith("Final report")]
    if cell[0] in ("me", "ss", "student"):
        s["todo"].append((cell[0], f"Final report {cell[1]}"))
    s["needs_me"] = any(k == "me" for k, _ in s["todo"])


def build_period(period, students, raw):
    now = datetime.now()
    today = (now.year, now.month)
    ym = month_years(raw)
    by_id = {r["id"]: r for r in raw}
    names = {m["month"] for r in raw for m in r["months"]} | {a["month"] for r in raw for a in r["asg"] if a["month"]}
    months = sorted(names, key=lambda m: ym.get(m, (9999, MONTHS.index(m) if m in MONTHS else 0)))
    out = []
    for st in students:
        r = by_id.get(st["emplId"], {"months": [], "asg": []})
        lb = {mo["month"]: logbook_cell(mo["days"], ym.get(mo["month"]), today) for mo in r["months"]}
        asg = {}
        for a in r["asg"]:
            asg.setdefault(a["month"], []).append(a)
        asg = {m: assignment_cell(rows, now) for m, rows in asg.items()}
        todo = [(c[0], f"{m[:3]} {kind} {c[1]}{' ' + c[2] if c[2] else ''}")
                for m in months for kind, c in (("LB", lb.get(m)), ("ASG", asg.get(m)))
                if c and c[0] in ("me", "ss", "student")]
        complete = bool(lb) and all(c[0] == "ok" for c in lb.values()) and all(
            assignment_kind(a, now)[0] == "ok" for a in r["asg"])
        out.append({"id": st["emplId"], "sid": st.get("studentId") or "", "name": st["studentName"].strip(),
                    "lb": lb, "asg": asg, "todo": todo, "complete": complete,
                    "needs_me": any(k == "me" for k, _ in todo), "fr": ("none", "—", "")})
    return {"v": period["v"], "t": period["t"], "months": months, "students": out, "groups": {}}


# ---------- tracker page ----------

TRACKER_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7280; --line:#e3e6eb;
  --ok:#e2f4e4; --okf:#1f6b2e; --me:#ffe98a; --mef:#5c4500; --ss:#ffe2c4; --ssf:#7a3d00;
  --st:#fde1e1; --stf:#8a1c1c; --pr:#e2ecfb; --prf:#1e4a8a; --fu:#f0f1f3; --fuf:#9aa0a8; }
@media (prefers-color-scheme: dark) { :root { --bg:#121417; --card:#1b1e23; --fg:#e8eaed; --muted:#9aa0a8; --line:#2c3138;
  --ok:#183a20; --okf:#9fe0ad; --me:#5a4a00; --mef:#ffe98a; --ss:#4a2d12; --ssf:#ffc890;
  --st:#4a1c1c; --stf:#ffb4b4; --pr:#1a2d4a; --prf:#a9c8ff; --fu:#23272d; --fuf:#6b7280; } }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
  font:14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size:20px; margin:0 0 2px; }
.muted { color:var(--muted); font-size:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin:12px 0; }
.card h2 { font-size:15px; margin:0 0 8px; }
.card ul { margin:0; padding-left:18px; }
.legend span { display:inline-block; margin:2px 6px 2px 0; padding:2px 6px; border-radius:6px; font-size:12px; }
details { background:var(--card); border:1px solid var(--line); border-radius:12px; margin:12px 0; overflow:hidden; }
summary { padding:12px 14px; font-weight:600; cursor:pointer; }
summary .muted { font-weight:400; display:block; }
.scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; border-top:1px solid var(--line); }
table { border-collapse:separate; border-spacing:0; font-size:12px; }
th, td { padding:5px 6px; border-bottom:1px solid var(--line); text-align:center; white-space:nowrap; }
thead th { background:var(--card); font-weight:600; color:var(--muted); }
th.name { position:sticky; left:0; z-index:1; background:var(--card); text-align:left; max-width:150px;
  white-space:normal; border-right:1px solid var(--line); font-weight:600; }
th.name small, td small { display:block; font-weight:400; color:var(--muted); font-size:10px; }
td small { color:inherit; opacity:.8; }
.m { border-left:1px solid var(--line); }
.ok { background:var(--ok); color:var(--okf); } .me { background:var(--me); color:var(--mef); font-weight:600; }
.ss { background:var(--ss); color:var(--ssf); } .student { background:var(--st); color:var(--stf); }
.progress { background:var(--pr); color:var(--prf); } .future, .none { background:var(--fu); color:var(--fuf); }
.tag { display:inline-block; font-size:10px; padding:1px 5px; border-radius:6px; margin-left:4px; font-weight:600; }
"""


def write_tracker(status, path):
    e = html.escape

    def td(c, first=False):
        kind, label, note = c
        return f'<td class="{kind}{" m" if first else ""}">{e(label)}{f"<small>{e(note)}</small>" if note else ""}</td>'

    none = ("none", "", "")
    needs, sections = [], []
    for i, per in enumerate(status):
        students = sorted(per["students"], key=lambda s: (not s["needs_me"], s["complete"], s["name"]))
        n_me = sum(s["needs_me"] for s in students)
        n_done = sum(s["complete"] for s in students)
        n_wait = sum(any(k == "student" for k, _ in s["todo"]) for s in students)
        for s in students:
            items = [t for k, t in s["todo"] if k == "me"]
            if items:
                needs.append(f"<li><b>{e(s['name'].title())}</b> <span class='muted'>({e(per['t'])})</span>: {e(', '.join(items))}</li>")
        head1 = "".join(f'<th colspan="2" class="m">{e(m[:3])}</th>' for m in per["months"])
        head2 = '<th class="m">LB</th><th>ASG</th>' * len(per["months"])
        rows = []
        for s in students:
            tag = ('<span class="tag me">needs you</span>' if s["needs_me"]
                   else '<span class="tag ok">done</span>' if s["complete"] else "")
            cells = "".join(td(s["lb"].get(m, none), True) + td(s["asg"].get(m, none)) for m in per["months"])
            rows.append(f'<tr><th class="name">{e(s["name"].title())}{tag}<small>{e(s["sid"])}</small></th>'
                        f'{cells}{td(s["fr"], True)}</tr>')
        sections.append(
            f'<details{" open" if i == 0 else ""}><summary>{e(per["t"])}<span class="muted">{len(students)} students · '
            f'{n_me} need you · {n_wait} waiting on students · {n_done} complete</span></summary>'
            f'<div class="scroll"><table><thead><tr><th class="name" rowspan="2">Student</th>{head1}'
            f'<th class="m" rowspan="2">Final<br>report</th></tr><tr>{head2}</tr></thead>'
            f'<tbody>{"".join(rows) or "<tr><td>No students</td></tr>"}</tbody></table></div></details>')
    legend = "".join(f'<span class="{k}">{e(t)}</span>' for k, t in [
        ("ok", "✅ approved"), ("me", "⏳ FS needs you"), ("me", "⏳ ext extension request"), ("ss", "⏳ SS site supervisor"),
        ("student", "❌ not submitted"), ("student", "✏️ rejected"), ("student", "🕒 extended, waiting"),
        ("progress", "📝 this month"), ("future", "⬜ not due yet")])
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BINUS AE Tracker</title>
<style>{TRACKER_CSS}</style></head><body>
<h1>BINUS AE Tracker</h1><div class="muted">Updated {datetime.now():%d %b %Y, %H:%M} · LB = logbook, ASG = assignment</div>
<div class="card"><h2>Needs you ({len(needs)})</h2>{f"<ul>{''.join(needs)}</ul>" if needs else '<div class="muted">Nothing — all caught up.</div>'}</div>
<div class="card legend">{legend}</div>
{"".join(sections)}
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(path)


# ---------- login ----------

def on_app(page):
    host = urlparse(page.url).hostname or ""
    if not host.startswith("activity-enrichment"):
        return False
    try:
        return page.get_by_role("link", name=re.compile(r"Logbook Activity", re.I)).count() > 0
    except PWError:
        return False


def handle_microsoft(page, email, password, tries):
    def vis(sel):
        loc = page.locator(sel).first
        return loc if loc.count() and loc.is_visible() else None

    tile = vis(f'[data-test-id="{email}"]') or vis(f'#tilesHolder :text("{email}")')
    if tile:
        log.info("Microsoft: picking account tile")
        tile.click()
        return
    # Microsoft keeps an invisible decoy of the other field (.moveOffScreen) that Playwright counts as visible.
    email_sel = 'input[name="loginfmt"]:not(.moveOffScreen)'
    pw_sel = 'input[name="passwd"]:not(.moveOffScreen)'
    nxt = vis("#idSIButton9")
    em = vis(email_sel)
    if em and tries["email"] < 2:
        log.info("Microsoft: entering email")
        tries["email"] += 1
        em.fill(email)
        if nxt:
            nxt.click()
        else:
            em.press("Enter")
        page.locator(pw_sel).first.wait_for(timeout=15000)
        return
    pw = vis(pw_sel)
    if pw and password and tries["pw"] < 2:
        log.info("Microsoft: entering password")
        tries["pw"] += 1
        pw.fill(password)
        if nxt:
            nxt.click()
        else:
            pw.press("Enter")
        pw.wait_for(state="hidden", timeout=15000)
        return
    kmsi = page.get_by_text(re.compile(r"Stay signed in", re.I)).first
    if nxt and tries["kmsi"] < 3 and kmsi.count() and kmsi.is_visible():
        log.info("Microsoft: stay signed in -> Yes")
        tries["kmsi"] += 1
        box = vis("#KmsiCheckboxField")
        if box:
            box.check()
        nxt.click()
        nxt.wait_for(state="hidden", timeout=15000)


def login(page, email, password, timeout=300):
    page.goto(f"{BASE}/Auth/FSLogin", wait_until="domcontentloaded")
    if not on_app(page):
        btn = page.locator("#btnLogin")
        if btn.count():
            btn.click()
    tries = {"email": 0, "pw": 0, "kmsi": 0}
    waited_msg = False
    end = time.time() + timeout
    while time.time() < end:
        try:
            if on_app(page):
                log.info("Logged in.")
                return
            host = urlparse(page.url).hostname or ""
            if "microsoft" in host or "live.com" in host:
                handle_microsoft(page, email, password, tries)
            if tries["pw"] and not waited_msg:
                log.info("Waiting for login to finish (approve MFA on your phone if asked)...")
                waited_msg = True
        except PWError:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError("Login did not complete in time")


def login_ss(page, username, password, timeout=120):
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    tried = False
    end = time.time() + timeout
    while time.time() < end:
        try:
            if on_app(page):
                log.info("Logged in.")
                return
            user = page.locator("#Username")
            if not tried and password and user.count() and user.is_visible():
                log.info("Site Supervisor: entering username and password")
                tried = True
                user.fill(username)
                page.locator("#Password").fill(password)
                page.locator("#btnLogin").click()
        except PWError:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError("Login did not complete in time")


def notify(text):
    if sys.platform != "darwin":
        return
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "BINUS Activity Enrichment"'],
                   capture_output=True)


# ---------- setup ----------

def default_tracker():
    """The BINUS OneDrive folder if it syncs on this computer (so the page opens on a phone), else the data folder."""
    home = Path.home()
    found = sorted(home.glob("Library/CloudStorage/OneDrive*Bina*")) + sorted(home.glob("OneDrive - Bina*"))
    return (found[0] if found else HOME) / "BINUS AE Tracker.html"


def saved_password(env, account):
    if env in os.environ:  # explicit override; an empty value means the password is never typed
        return os.environ[env]
    try:
        return keyring.get_password(KEYRING, account) or ""
    except KeyringError:
        return ""


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install_reminder():
    exe = shutil.which("binus-ae") or str(Path(sys.argv[0]).absolute())
    cmd = HOME / "run.command"
    cmd.write_text(f"#!/bin/zsh\nexec {shlex.quote(exe)}\n")
    cmd.chmod(0o755)
    script = (
        f'[[ "$(cat {shlex.quote(str(STATE))} 2>/dev/null)" == "$(date +%Y-%m)" ]] && exit 0\n'
        "a=$(osascript -e 'button returned of (display dialog \"Time for the monthly BINUS Activity Enrichment "
        "approvals. Run them now?\" with title \"BINUS Activity Enrichment\" buttons {\"Later\", \"Run\"} "
        "default button \"Run\" giving up after 3600)')\n"
        f"[[ $a == Run ]] && open -a Terminal {shlex.quote(str(cmd))}\n"
    )
    REMINDER.parent.mkdir(parents=True, exist_ok=True)
    with REMINDER.open("wb") as f:
        plistlib.dump({"Label": REMINDER_LABEL, "ProgramArguments": ["/bin/zsh", "-c", script],
                       "StartCalendarInterval": [{"Day": d, "Hour": 9, "Minute": 0} for d in range(5, 10)]}, f)
    launchctl("bootout", f"gui/{os.getuid()}/{REMINDER_LABEL}")
    r = launchctl("bootstrap", f"gui/{os.getuid()}", str(REMINDER))
    print("Monthly reminder installed." if r.returncode == 0 else f"Could not load the reminder: {r.stderr.strip()}")


def remove_reminder():
    if REMINDER.exists():
        launchctl("bootout", f"gui/{os.getuid()}/{REMINDER_LABEL}")
        REMINDER.unlink()
        print("Monthly reminder removed.")


def setup():
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)  # browser sessions and logs live here
    print("Installing the WebKit browser used for the automation...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "webkit"], check=True)
    load_dotenv(CONFIG)
    cfg = {}

    def ask(key, label, default=""):
        current = os.getenv(key) or default
        cfg[key] = input(f"{label} [{current}]: ").strip() or current
        return cfg[key]

    def ask_password(account, label):
        pw = getpass.getpass(f"{label} (Enter to keep the saved one / skip): ")
        if pw:
            keyring.set_password(KEYRING, account, pw)

    print("\nPasswords are stored in the system keychain, never in a file.")
    email = ask("BINUS_EMAIL", "Faculty Supervisor BINUS email (empty if you are not one)")
    if email:
        ask_password(f"fs:{email}", "Microsoft password")
    ss_user = ask("SS_USERNAME", "Site Supervisor username (empty if you are not one)")
    if ss_user:
        ask_password(f"ss:{ss_user}", "Site Supervisor password")
    ask("FEEDBACK", "Feedback text for approved logbooks", "ok")
    ask("PERIODS", "How many recent periods to check", "3")
    ask("TRACKER_PATH", "Where to save the tracker page", str(default_tracker()))

    def quote(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    CONFIG.write_text("".join(f"{k}={quote(v)}\n" for k, v in cfg.items() if v))
    CONFIG.chmod(0o600)
    print(f"Settings saved to {CONFIG}")
    if sys.platform == "darwin":
        if input("Remind me on days 5-9 of every month at 09:00? [Y/n]: ").strip().lower() in ("", "y", "yes"):
            install_reminder()
        else:
            remove_reminder()
    print("\nDone. Next: run `binus-ae --dry-run` (browser visible) to sign in once and preview what it would do.")


def main():
    ap = argparse.ArgumentParser(description="BINUS Activity Enrichment approvals and progress tracker.")
    ap.add_argument("command", nargs="?", choices=["run", "setup"], default="run",
                    help="setup = first-time configuration (default: run)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done, submit nothing")
    ap.add_argument("--role", choices=["fs", "ss"], help="only Faculty Supervisor or only Site Supervisor (default: both)")
    ap.add_argument("--only", choices=["logbook", "assignment", "final-report"])
    ap.add_argument("--limit", type=int, help="max students per section")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--periods", type=int, nargs="+", metavar="N",
                    help="periods to process, 1 = latest (default: the newest PERIODS from setup)")
    ap.add_argument("--report", action="store_true", help="only refresh the tracker page, approve nothing")
    args = ap.parse_args()

    if args.command == "setup":
        setup()
        return 0
    load_dotenv(CONFIG)
    email = os.getenv("BINUS_EMAIL", "")
    ss_user = os.getenv("SS_USERNAME", "")
    if not email and not ss_user:
        print("Not set up yet. Run: binus-ae setup")
        return 2
    password = saved_password("BINUS_PASSWORD", f"fs:{email}")
    ss_password = saved_password("SS_PASSWORD", f"ss:{ss_user}")
    periods = args.periods or range(1, int(os.getenv("PERIODS", "3")) + 1)
    tracker = Path(os.getenv("TRACKER_PATH") or default_tracker()).expanduser()

    LOGS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    for h in (logging.FileHandler(LOGS / f"{datetime.now():%Y-%m-%d}.log"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.info("Run started%s", " (DRY RUN)" if args.dry_run else "")

    jobs = []
    if args.role in (None, "ss") and args.only in (None, "logbook") and ss_user and not args.report:
        jobs.append(("ss", PROFILE_SS, lambda page: login_ss(page, ss_user, ss_password)))
    if args.role in (None, "fs") and email:
        jobs.append(("fs", PROFILE, lambda page: login(page, email, password)))

    bots, login_failed, tracker_written = {}, [], False
    with sync_playwright() as pw:
        for role, profile, do_login in jobs:
            log.info("##### %s #####", "SITE SUPERVISOR" if role == "ss" else "FACULTY SUPERVISOR")
            try:
                ctx = pw.webkit.launch_persistent_context(
                    str(profile), headless=args.headless, viewport={"width": 1440, "height": 900}
                )
            except PWError as e:
                log.error("Could not start the browser (run `binus-ae setup`): %s", str(e).splitlines()[0])
                login_failed.append(role.upper())
                continue
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            try:
                do_login(page)
            except Exception as e:
                log.error("Login failed: %s", e)
                login_failed.append(role.upper())
                ctx.close()
                continue
            bot = bots[role] = Bot(page, args, ss=role == "ss")
            try:
                if role == "ss":
                    bot.run_logbook(periods)
                elif args.report:
                    bot.collect_status(periods)
                else:
                    if args.only in (None, "logbook"):
                        bot.run_logbook(periods)
                    if args.only in (None, "assignment"):
                        bot.run_assignment(periods)
                    # Status is collected after approving, so the final report check and the tracker see the latest state.
                    bot.collect_status(periods)
                    if args.only in (None, "final-report"):
                        bot.run_final_report(periods)
                        if bot.stats["fr_groups"]:
                            bot.collect_groups(periods)
                if bot.status:
                    write_tracker(bot.status, tracker)
                    tracker_written = True
                    log.info("Tracker updated: %s", tracker)
            except Exception as e:
                bot.stats["errors"] += 1
                log.exception("Fatal: %s", e)
            finally:
                ctx.close()

    parts = []
    if "ss" in bots:
        s = bots["ss"].stats
        parts.append(f"SS logbook: {s['logbook_months']} month(s) approved for {s['logbook_students']} student(s)")
    if "fs" in bots and not args.report:
        s = bots["fs"].stats
        parts.append(
            f"FS logbook: {s['logbook_months']} month(s) approved for {s['logbook_students']} student(s); "
            f"Assignment: {s['asg_approved']} approved, {s['asg_extended']} extended; "
            f"Final report: {s['fr_groups']} group(s) approved"
        )
    errors = sum(b.stats["errors"] for b in bots.values())
    parts.append(f"errors: {errors}")
    if login_failed:
        parts.append(f"login failed: {', '.join(login_failed)}")
    if tracker_written:
        parts.append("tracker updated")
    summary = "; ".join(parts)
    log.info(summary)
    full_run = not (args.dry_run or args.report or args.only or args.role or args.periods) and args.limit is None
    if full_run and not errors and not login_failed:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "last_success").write_text(f"{datetime.now():%Y-%m}\n")
    print(f"SUMMARY: {'[dry-run] ' if args.dry_run else ''}{summary}")
    notify(f"{'[dry-run] ' if args.dry_run else ''}{summary}")
    if login_failed and not bots:
        return 2
    return 1 if errors or login_failed else 0


if __name__ == "__main__":
    sys.exit(main())
