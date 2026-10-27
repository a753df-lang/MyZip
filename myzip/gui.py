"""MyZip GUI (tkinter)."""
import os
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from collections import deque
from tkinter import filedialog, messagebox, ttk

from . import __version__, core, update
from .progress import Cancelled, Progress

APP = "MyZip"


class CreateDialog(tk.Toplevel):
    """새 압축 파일 만들기 창."""

    def __init__(self, master, sources=None):
        super().__init__(master)
        self.title("새로 압축")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self.sources = list(sources or [])

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="압축할 파일/폴더").grid(row=0, column=0, sticky="w")
        self.lst = tk.Listbox(frm, width=60, height=8)
        self.lst.grid(row=1, column=0, columnspan=3, sticky="we")
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 10))
        ttk.Button(btns, text="파일 추가", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="폴더 추가", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(btns, text="제거", command=self.remove).pack(side="left")

        ttk.Label(frm, text="형식").grid(row=3, column=0, sticky="w")
        self.fmt = tk.StringVar(value="zip")
        cb = ttk.Combobox(frm, textvariable=self.fmt, values=list(core.FORMATS), state="readonly", width=14)
        cb.grid(row=3, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self.fix_ext())

        ttk.Label(frm, text="압축 레벨 (0=저장, 9=최고)").grid(row=4, column=0, sticky="w", pady=4)
        self.level = tk.IntVar(value=6)
        ttk.Spinbox(frm, from_=0, to=9, textvariable=self.level, width=5).grid(row=4, column=1, sticky="w")

        ttk.Label(frm, text="저장 위치").grid(row=5, column=0, sticky="w")
        self.out = tk.StringVar()
        ttk.Entry(frm, textvariable=self.out, width=48).grid(row=6, column=0, columnspan=2, sticky="we")
        ttk.Button(frm, text="찾아보기…", command=self.browse_out).grid(row=6, column=2, padx=(4, 0))

        act = ttk.Frame(frm)
        act.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(act, text="압축 시작", command=self.ok).pack(side="left")
        ttk.Button(act, text="취소", command=self.destroy).pack(side="left", padx=(4, 0))

        for s in self.sources:
            self.lst.insert("end", s)
        self.suggest_out()
        self.grab_set()

    def add_files(self):
        for f in filedialog.askopenfilenames(parent=self):
            self._add(f)

    def add_folder(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self._add(d)

    def _add(self, path):
        path = os.path.normpath(path)
        if path not in self.sources:
            self.sources.append(path)
            self.lst.insert("end", path)
            self.suggest_out()

    def remove(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)
            del self.sources[i]

    def suggest_out(self):
        if self.out.get() or not self.sources:
            return
        first = self.sources[0]
        name = os.path.basename(first.rstrip("\\/"))
        if len(self.sources) == 1 and os.path.isfile(first):
            name = os.path.splitext(name)[0]
        self.out.set(os.path.join(os.path.dirname(first), name + core.FORMATS[self.fmt.get()]))

    def fix_ext(self):
        out = self.out.get()
        if not out:
            return
        for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tar"):
            if out.lower().endswith(ext):
                out = out[: -len(ext)]
                break
        self.out.set(out + core.FORMATS[self.fmt.get()])

    def browse_out(self):
        ext = core.FORMATS[self.fmt.get()]
        f = filedialog.asksaveasfilename(parent=self, defaultextension=ext,
                                         filetypes=[(self.fmt.get(), "*" + ext), ("모든 파일", "*.*")])
        if f:
            self.out.set(f)

    def ok(self):
        if not self.sources:
            messagebox.showwarning(APP, "압축할 파일을 추가하세요.", parent=self)
            return
        if not self.out.get():
            messagebox.showwarning(APP, "저장 위치를 지정하세요.", parent=self)
            return
        if os.path.exists(self.out.get()) and not messagebox.askyesno(
                APP, "파일이 이미 있습니다. 덮어쓸까요?", parent=self):
            return
        self.result = (self.sources, self.out.get(), self.fmt.get(), int(self.level.get()))
        self.destroy()


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60:02d}초"
    return f"{sec // 3600}시간 {sec % 3600 // 60:02d}분"


class ProgressDialog(tk.Toplevel):
    """진행 창: 진행 막대, 속도 그래프, 경과/남은 시간, 속도, 일시 정지/취소.

    작업이 금방 끝나면 창이 깜빡이지 않도록 0.3초 뒤에야 보여 준다.
    """
    W, H = 440, 120  # 그래프 크기

    def __init__(self, master, title, progress):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.p = progress
        self.closed = False
        self.samples = []      # 그래프 점: (진행 비율, 속도)
        self.recent = deque()  # 최근 2초 (경과 시간, 처리한 바이트) → 현재 속도
        self.peak = 1.0

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        top = ttk.Frame(frm)
        top.pack(fill="x")
        self.pct = ttk.Label(top, text="0%", font=("Segoe UI", 20, "bold"))
        self.pct.pack(side="left")
        self.state_lbl = ttk.Label(top, text="준비 중…", foreground="#555")
        self.state_lbl.pack(side="left", padx=(12, 0), pady=(8, 0))
        self.bar = ttk.Progressbar(frm, length=self.W, maximum=1000)
        self.bar.pack(fill="x", pady=(6, 8))

        self.graph = tk.Canvas(frm, width=self.W, height=self.H, bg="white",
                               highlightthickness=1, highlightbackground="#c8c8c8")
        self.graph.pack()

        info = ttk.Frame(frm)
        info.pack(fill="x", pady=(10, 0))
        self.vals = {}
        for i, key in enumerate(("경과 시간", "남은 시간", "속도", "처리한 크기", "파일")):
            r, c = divmod(i, 2)
            ttk.Label(info, text=key, foreground="#666").grid(row=r, column=c * 2, sticky="w", padx=(0, 10))
            self.vals[key] = ttk.Label(info, text="-")
            self.vals[key].grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 28))
        self.name_lbl = ttk.Label(frm, text="", foreground="#555", width=60)
        self.name_lbl.pack(fill="x", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        self.cancel_btn = ttk.Button(btns, text="취소", command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.pause_btn = ttk.Button(btns, text="일시 정지", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 6))

        self.after(300, self._show)
        self.after(150, self.refresh)

    def _show(self):
        if not self.closed:
            self.deiconify()
            self.lift()

    def toggle_pause(self):
        if self.p.paused:
            self.p.resume()
            self.pause_btn.config(text="일시 정지")
        else:
            self.p.pause()
            self.pause_btn.config(text="계속")
        self.refresh(reschedule=False)

    def cancel(self):
        self.p.cancel()
        self.state_lbl.config(text="취소하는 중…")
        self.pause_btn.state(["disabled"])
        self.cancel_btn.state(["disabled"])

    def fraction(self) -> float:
        p = self.p
        if p.total_bytes:
            return min(p.done_bytes / p.total_bytes, 1.0)
        return p.done_items / p.total_items if p.total_items else 0.0

    def refresh(self, reschedule=True):
        if self.closed:
            return
        p = self.p
        now, done = p.elapsed(), p.done_bytes
        self.recent.append((now, done))
        while len(self.recent) > 2 and now - self.recent[0][0] > 2.0:
            self.recent.popleft()
        t0, b0 = self.recent[0]
        speed = (done - b0) / (now - t0) if now - t0 > 0.2 else 0.0
        frac = self.fraction()

        if not p.paused:
            if self.samples and int(self.samples[-1][0] * self.W) == int(frac * self.W):
                self.samples[-1] = (frac, speed)  # 같은 가로 위치면 마지막 점을 바꾼다
            else:
                self.samples.append((frac, speed))
            self.peak = max(self.peak, speed)

        self.pct.config(text=f"{frac * 100:.0f}%")
        self.bar["value"] = frac * 1000
        if p.cancelled:
            pass
        elif p.paused:
            self.state_lbl.config(text="일시 정지됨")
        else:
            self.state_lbl.config(text="")
        avg = done / now if now > 0 else 0
        self.vals["경과 시간"].config(text=_fmt_time(now))
        left = (p.total_bytes - done) / avg if avg and now > 1 else None
        self.vals["남은 시간"].config(text=_fmt_time(left) if left is not None else "계산 중…")
        self.vals["속도"].config(text="-" if p.paused else f"{core.human_size(int(speed))}/s")
        self.vals["처리한 크기"].config(text=f"{core.human_size(done)} / {core.human_size(p.total_bytes)}")
        self.vals["파일"].config(text=f"{p.done_items:,} / {p.total_items:,}")
        self.name_lbl.config(text=p.name[-70:])
        self._draw(speed)
        if reschedule:
            self.after(150, self.refresh)

    def _draw(self, speed):
        g, W, H = self.graph, self.W, self.H
        g.delete("all")
        for i in range(1, 4):  # 25% 간격 세로 눈금
            g.create_line(W * i / 4, 0, W * i / 4, H, fill="#ececec")
        top = self.peak * 1.15
        y = lambda s: H - 2 - (H - 14) * s / top  # noqa: E731
        if self.samples:
            pts = [0, H]
            for frac, s in self.samples:
                pts += [frac * W, y(s)]
            pts += [self.samples[-1][0] * W, H]
            g.create_polygon(pts, fill="#cfeccd", outline="")
            if len(self.samples) >= 2:
                g.create_line(pts[2:-2], fill="#2e9e44", width=2)
            cy = y(speed)
            g.create_line(0, cy, W, cy, fill="#7a7a7a", dash=(3, 3))
            label = f"속도: {core.human_size(int(speed))}/s"
            g.create_text(W - 6, max(cy - 8, 8), text=label, anchor="e", fill="#444",
                          font=("Segoe UI", 9))

    def close(self):
        self.closed = True
        self.destroy()


class App(tk.Tk):
    def __init__(self, open_path=None):
        super().__init__()
        self.title(APP)
        if getattr(sys, "frozen", False):  # exe로 실행 중이면 exe에 넣은 아이콘을 창에도 사용
            try:
                self.iconbitmap(sys.executable)
            except tk.TclError:
                pass
        self.geometry("860x520")
        self.minsize(600, 320)
        self.archive = None
        self.busy = False
        self.dialog = None  # 작업 중일 때 진행 창
        self.events = queue.Queue()

        self._build_menu()
        self._build_toolbar()
        self._build_list()
        self._build_status()
        self.after(50, self._poll)
        # 다른 창 뒤에 숨어서 열리지 않도록 처음 한 번 앞으로 가져온다
        self.after(150, self._bring_to_front)
        # 시작할 때 조용히 새 버전 확인 (새 버전이 있을 때만 알림)
        self.after(1500, lambda: self.check_update(silent=True))

        if open_path:
            if os.path.isdir(open_path) or not self._is_archive(open_path):
                self.after(100, lambda: self.new_archive([open_path]))
            else:
                self.after(100, lambda: self.open_archive(open_path))

    def _bring_to_front(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    # ---------- UI ----------
    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=False)
        fm.add_command(label="압축 파일 열기…", accelerator="Ctrl+O", command=self.open_dialog)
        fm.add_command(label="새로 압축…", accelerator="Ctrl+N", command=self.new_archive)
        fm.add_separator()
        fm.add_command(label="종료", command=self.destroy)
        m.add_cascade(label="파일", menu=fm)
        am = tk.Menu(m, tearoff=False)
        am.add_command(label="폴더 선택해서 풀기…", command=self.extract_all)
        am.add_command(label="같은 폴더에 풀기", command=self.extract_same_folder)
        am.add_command(label="'압축파일 이름' 폴더에 풀기", command=self.extract_here)
        am.add_command(label="선택 항목 풀기…", command=self.extract_selected)
        am.add_command(label="압축 파일 테스트", command=self.test_archive)
        m.add_cascade(label="작업", menu=am)
        hm = tk.Menu(m, tearoff=False)
        hm.add_command(label="업데이트 확인…", command=self.check_update)
        hm.add_command(label="MyZip 정보", command=self.show_about)
        m.add_cascade(label="도움말", menu=hm)
        self.config(menu=m)
        self.bind("<Control-o>", lambda e: self.open_dialog())
        self.bind("<Control-n>", lambda e: self.new_archive())

    def _build_toolbar(self):
        tb = ttk.Frame(self, padding=(6, 6))
        tb.pack(fill="x")
        for text, cmd in (("📂 열기", self.open_dialog), ("➕ 새로 압축", self.new_archive),
                          ("📤 폴더 선택해서 풀기", self.extract_all),
                          ("📂 같은 폴더에 풀기", self.extract_same_folder),
                          ("📁 이름 폴더에 풀기", self.extract_here),
                          ("✅ 테스트", self.test_archive)):
            ttk.Button(tb, text=text, command=cmd).pack(side="left", padx=2)

    def _build_list(self):
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=6)
        cols = ("size", "packed", "ratio", "modified")
        self.tree = ttk.Treeview(frame, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="이름", command=lambda: self._sort("#0"))
        for c, label, w in (("size", "크기", 100), ("packed", "압축 크기", 100),
                            ("ratio", "압축률", 70), ("modified", "수정한 날짜", 150)):
            self.tree.heading(c, text=label, command=lambda c=c: self._sort(c))
            self.tree.column(c, width=w, anchor="e" if c != "modified" else "w", stretch=False)
        self.tree.column("#0", width=380)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)

    def _on_double_click(self, event):
        # 폴더는 기본 동작(펼치기/접기)만 하고, 파일을 더블클릭했을 때만 풀기
        item = self.tree.identify_row(event.y)
        if item and not self.tree.get_children(item):
            self.extract_selected()

    def _build_status(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="압축 파일을 열거나 새로 압축하세요.")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        self.prog = ttk.Progressbar(bar, length=220, mode="determinate")
        self.prog.pack(side="right")

    # ---------- 동작 ----------
    @staticmethod
    def _is_archive(path):
        try:
            core.detect_kind(path)
            return True
        except core.ArchiveError:
            # 손상되었거나 조각이 빠진 압축 파일은 새로 압축하지 말고 열기에서 오류를 보여 준다
            return re.search(r"\.(zip|z\d{2,}|\d{3})$", path, re.I) is not None
        except OSError:
            return False

    def open_dialog(self):
        f = filedialog.askopenfilename(filetypes=[
            ("압축 파일", "*.zip *.z01 *.001 *.tar *.tar.gz *.tgz *.tar.bz2 *.tar.xz"), ("모든 파일", "*.*")])
        if f:
            self.open_archive(f)

    def open_archive(self, path):
        try:
            entries = core.list_entries(path)
        except (core.ArchiveError, OSError) as e:
            messagebox.showerror(APP, str(e))
            return
        self.archive = path
        self.title(f"{os.path.basename(path)} - {APP}")
        self._fill_tree(entries)
        total = sum(e.size for e in entries)
        self.status.set(f"{len(entries)}개 항목, 원본 {core.human_size(total)}, "
                        f"압축 파일 {core.human_size(core.archive_size(path))}")

    def _fill_tree(self, entries):
        self.tree.delete(*self.tree.get_children())
        nodes = {"": ""}

        def ensure_dir(path):  # path는 "a/b/" 형태
            if path in nodes:
                return nodes[path]
            parent = path.rstrip("/").rpartition("/")[0]
            parent = parent + "/" if parent else ""
            pid = ensure_dir(parent)
            name = path.rstrip("/").rpartition("/")[2]
            nodes[path] = self.tree.insert(pid, "end", iid=path, text="📁 " + name,
                                           values=("", "", "", ""), open=False)
            return nodes[path]

        for e in entries:
            if e.is_dir:
                ensure_dir(e.name)
                continue
            parent = e.name.rpartition("/")[0]
            pid = ensure_dir(parent + "/") if parent else ""
            when = e.modified.strftime("%Y-%m-%d %H:%M") if e.modified else ""
            self.tree.insert(pid, "end", iid=e.name, text="📄 " + e.name.rpartition("/")[2],
                             values=(core.human_size(e.size), core.human_size(e.packed),
                                     f"{e.ratio:.0f}%", when))

    def _sort(self, col):
        def key(iid):
            return self.tree.item(iid, "text") if col == "#0" else str(self.tree.set(iid, col))
        for parent in [""] + list(self._all_items()):
            kids = list(self.tree.get_children(parent))
            for i, k in enumerate(sorted(kids, key=key)):
                self.tree.move(k, parent, i)

    def _all_items(self, parent=""):
        for c in self.tree.get_children(parent):
            yield c
            yield from self._all_items(c)

    def _need_archive(self):
        if not self.archive:
            messagebox.showinfo(APP, "먼저 압축 파일을 여세요.")
            return False
        return not self.busy

    def extract_all(self):
        """고른 폴더 안에 '압축 파일 이름' 폴더를 만들어 풀기 (사과.zip → 고른폴더\\사과\\)."""
        if not self._need_archive():
            return
        picked = filedialog.askdirectory(title="압축을 풀 폴더 선택",
                                         initialdir=os.path.dirname(os.path.abspath(self.archive)))
        if not picked:
            return
        try:
            dest = core.name_folder_dest(self.archive, picked)
        except (core.ArchiveError, OSError) as e:
            messagebox.showerror(APP, str(e))
            return
        self._run_extract(dest, self._selection_or_none())

    def extract_same_folder(self):
        """압축 파일이 있는 폴더에 바로 풀기 (하위 폴더 만들지 않음)."""
        if self._need_archive():
            self._run_extract(os.path.dirname(os.path.abspath(self.archive)), self._selection_or_none())

    def extract_here(self):
        """압축 파일 이름으로 된 폴더를 만들어 그 안에 풀기 (최상위 폴더가 하나뿐이면 그 폴더 그대로)."""
        if not self._need_archive():
            return
        try:
            dest = core.name_folder_dest(self.archive)
        except (core.ArchiveError, OSError) as e:
            messagebox.showerror(APP, str(e))
            return
        self._run_extract(dest, self._selection_or_none())

    def _selection_or_none(self):
        # 목록에서 선택한 항목이 있으면 그것만, 없으면 전체
        return list(self.tree.selection()) or None

    def extract_selected(self):
        if not self._need_archive():
            return
        sel = list(self.tree.selection())
        if not sel:
            return self.extract_all()
        dest = filedialog.askdirectory(title="선택 항목을 풀 폴더 선택")
        if dest:
            self._run_extract(dest, sel)

    def _run_extract(self, dest, members):
        archive = self.archive
        try:
            existing = core.conflicts(archive, dest, members)
        except (core.ArchiveError, OSError) as e:
            messagebox.showerror(APP, str(e))
            return
        if existing:
            shown = "\n".join(existing[:10]) + (f"\n… 외 {len(existing) - 10}개" if len(existing) > 10 else "")
            if not messagebox.askyesno(APP, f"이미 있는 파일 {len(existing)}개를 덮어씁니다.\n\n{shown}\n\n계속할까요?"):
                return

        def job(progress):
            n = core.extract(archive, dest, members, progress)
            return f"{n}개 항목을 풀었습니다 → {dest}", dest
        self._start(job, "압축 푸는 중", archive)

    def test_archive(self):
        if not self._need_archive():
            return
        archive = self.archive

        def job(progress):
            bad = core.test(archive, progress)
            if bad:
                raise core.ArchiveError("손상된 항목:\n" + "\n".join(bad[:20]))
            return "압축 파일에 이상이 없습니다.", None
        self._start(job, "테스트 중", archive)

    def new_archive(self, sources=None):
        if self.busy:
            return
        dlg = CreateDialog(self, sources)
        self.wait_window(dlg)
        if not dlg.result:
            return
        srcs, out, fmt, level = dlg.result

        def job(progress):
            n = core.create(srcs, out, fmt, level, progress)
            return f"{n}개 항목을 압축했습니다 → {os.path.basename(out)}", ("open", out)
        self._start(job, "압축하는 중", out)

    # ---------- 버전 / 업데이트 ----------
    def show_about(self):
        messagebox.showinfo(APP, f"{APP} {__version__}\n\n멀티코어 ZIP 압축 프로그램\n"
                                 "지원 형식: ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ")

    def check_update(self, silent=False):
        """새 버전 확인. silent=True(시작 시 자동 확인)면 새 버전이 있을 때만 알린다."""
        if not update.is_configured():
            if not silent:
                messagebox.showinfo(APP, f"현재 버전: {__version__}\n\n"
                                         "업데이트 서버(GitHub 저장소)가 아직 설정되지 않았습니다.")
            return

        events = self.events  # 작업 스레드가 창(self)을 붙잡지 않게 큐만 넘긴다

        def run():
            try:
                events.put(("update", update.check(), silent))
            except update.UpdateError as e:
                events.put(("update_error", str(e), silent))
        threading.Thread(target=run, daemon=True).start()

    def _on_update_result(self, info, silent):
        if info is None:
            if not silent:
                messagebox.showinfo(APP, f"최신 버전을 사용 중입니다. ({__version__})")
            return
        notes = info["notes"][:500]
        msg = (f"새 버전이 있습니다!\n\n현재: {__version__}\n최신: {info['version']}"
               + (f"\n\n{notes}" if notes else "") + "\n\n다운로드 페이지를 열까요?")
        if messagebox.askyesno(APP, msg):
            webbrowser.open(info["download"] or info["url"])

    # ---------- 백그라운드 작업 ----------
    def _start(self, job, label, target):
        self.busy = True
        self.status.set(label)
        self.prog["value"] = 0
        prog = Progress()
        self.dialog = ProgressDialog(self, f"{label} - {os.path.basename(target)}", prog)

        events = self.events  # 작업 스레드가 창(self)을 붙잡지 않게 큐만 넘긴다

        def run():
            try:
                msg, extra = job(prog)
                # 결과 메시지에 걸린 시간과 평균 속도를 덧붙인다
                secs = prog.elapsed()
                msg += f"\n\n걸린 시간 {_fmt_time(secs) if secs >= 1 else f'{secs:.1f}초'}"
                if secs > 0 and prog.done_bytes:
                    msg += f" · 평균 {core.human_size(int(prog.done_bytes / secs))}/s"
                events.put(("done", msg, extra))
            except Cancelled:
                events.put(("cancelled",))
            except Exception as e:  # 작업 스레드 오류를 UI로 전달
                events.put(("error", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _finish(self):
        self.busy = False
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "update":
                    self._on_update_result(ev[1], ev[2])
                elif ev[0] == "update_error":
                    if not ev[2]:
                        messagebox.showwarning(APP, ev[1])
                elif ev[0] == "done":
                    self._finish()
                    _, msg, extra = ev
                    self.prog["value"] = 100
                    self.status.set(msg.splitlines()[0])
                    if isinstance(extra, tuple) and extra[0] == "open":
                        self.open_archive(extra[1])
                        self.status.set(msg.splitlines()[0])
                    elif extra and os.path.isdir(extra) and messagebox.askyesno(APP, msg + "\n\n폴더를 열까요?"):
                        os.startfile(extra)
                    elif not extra:
                        messagebox.showinfo(APP, msg)
                elif ev[0] == "cancelled":
                    self._finish()
                    self.prog["value"] = 0
                    self.status.set("작업을 취소했습니다.")
                elif ev[0] == "error":
                    self._finish()
                    self.prog["value"] = 0
                    self.status.set("오류")
                    messagebox.showerror(APP, ev[1])
        except queue.Empty:
            pass
        if self.dialog:  # 아래 상태 줄의 막대도 같이 움직인다
            self.prog["value"] = self.dialog.fraction() * 100
        self.after(50, self._poll)


def _enable_hidpi():
    """고해상도 모니터에서 글자가 작거나 흐리게 보이지 않도록 Windows DPI 인식 켜기."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main(path=None):
    _enable_hidpi()
    App(path).mainloop()
