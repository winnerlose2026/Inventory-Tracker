#!/usr/bin/env python3
"""Inventory Tracker - Tkinter desktop GUI (zero extra dependencies)"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from inventory_tracker import (
    load_inventory, load_usage, save_inventory,
    add_item, update_item, record_usage, restock, remove_item,
)

# ---------------------------------------------------------------------------
# Theme colours
# ---------------------------------------------------------------------------
BG       = "#0f1117"
SURFACE  = "#1a1d27"
SURFACE2 = "#222635"
BORDER   = "#2e3350"
ACCENT   = "#4f8ef7"
GREEN    = "#22c55e"
RED      = "#ef4444"
YELLOW   = "#f59e0b"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"
FONT     = ("Segoe UI", 10)
FONT_B   = ("Segoe UI", 10, "bold")
FONT_H   = ("Segoe UI", 13, "bold")


def _style():
    s = ttk.Style()
    s.theme_use("clam")
    s.configure(".",          background=BG,      foreground=TEXT,   font=FONT)
    s.configure("TFrame",     background=BG)
    s.configure("TLabel",     background=BG,      foreground=TEXT,   font=FONT)
    s.configure("TButton",    background=SURFACE2, foreground=TEXT,  font=FONT,
                borderwidth=1, relief="flat", padding=(10, 5))
    s.map("TButton",
          background=[("active", BORDER)],
          foreground=[("active", TEXT)])
    s.configure("Accent.TButton", background=ACCENT,  foreground="white")
    s.map("Accent.TButton",  background=[("active", "#3a7ce0")])
    s.configure("Green.TButton",  background="#1a3d2a", foreground=GREEN,
                borderwidth=1, relief="flat", padding=(8, 4))
    s.map("Green.TButton",   background=[("active", "#22533a")])
    s.configure("Red.TButton",    background="#3d1a1a", foreground=RED,
                borderwidth=1, relief="flat", padding=(8, 4))
    s.map("Red.TButton",     background=[("active", "#531a1a")])
    s.configure("TNotebook",        background=BG,    borderwidth=0)
    s.configure("TNotebook.Tab",    background=SURFACE, foreground=MUTED,
                padding=(14, 8), font=FONT)
    s.map("TNotebook.Tab",
          background=[("selected", BG)],
          foreground=[("selected", ACCENT)])
    s.configure("Treeview",         background=SURFACE, foreground=TEXT,
                fieldbackground=SURFACE, rowheight=26, font=FONT,
                borderwidth=0, relief="flat")
    s.configure("Treeview.Heading", background=SURFACE2, foreground=MUTED,
                font=("Segoe UI", 9, "bold"), relief="flat")
    s.map("Treeview", background=[("selected", SURFACE2)],
                      foreground=[("selected", ACCENT)])
    s.configure("TEntry",           background=SURFACE2, foreground=TEXT,
                insertcolor=TEXT,   fieldbackground=SURFACE2,
                borderwidth=1,      relief="flat", padding=(6, 4))
    s.configure("TCombobox",        background=SURFACE2, foreground=TEXT,
                fieldbackground=SURFACE2, selectbackground=SURFACE2,
                selectforeground=TEXT)
    s.configure("TLabelframe",      background=BG,    bordercolor=BORDER,
                relief="flat")
    s.configure("TLabelframe.Label", background=BG,  foreground=MUTED, font=FONT)
    s.configure("TSeparator",        background=BORDER)
    return s


# ---------------------------------------------------------------------------
# Reusable dialog for add / edit
# ---------------------------------------------------------------------------
class ItemDialog(tk.Toplevel):
    def __init__(self, parent, title="Item", data=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None

        fields = [
            ("Name",           "name",      data.get("name", "")           if data else ""),
            ("Quantity",       "quantity",  str(data.get("quantity", ""))   if data else ""),
            ("Unit",           "unit",      data.get("unit", "")            if data else ""),
            ("Category",       "category",  data.get("category", "general") if data else "general"),
            ("Price/unit ($)", "price",     str(data.get("price", "0"))     if data else "0"),
            ("Low stock threshold", "low_stock_threshold",
             str(data.get("low_stock_threshold", "5"))                      if data else "5"),
        ]

        self._vars = {}
        for row, (label, key, val) in enumerate(fields):
            tk.Label(self, text=label, bg=BG, fg=MUTED, font=FONT).grid(
                row=row, column=0, sticky="w", padx=18, pady=6)
            var = tk.StringVar(value=val)
            self._vars[key] = var
            e = ttk.Entry(self, textvariable=var, width=28)
            e.grid(row=row, column=1, padx=(4, 18), pady=6)
            if row == 0:
                e.focus_set()
                if data:
                    e.configure(state="disabled")

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=len(fields), column=0, columnspan=2, pady=(6, 14))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="left", padx=6)

        self.bind("<Return>", lambda _: self._save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()
        self.transient(parent)
        self.wait_window()

    def _save(self):
        v = {k: var.get().strip() for k, var in self._vars.items()}
        if not v["name"] or not v["quantity"] or not v["unit"]:
            messagebox.showerror("Missing fields", "Name, quantity and unit are required.", parent=self)
            return
        try:
            v["quantity"] = float(v["quantity"])
            v["price"] = float(v["price"] or 0)
            v["low_stock_threshold"] = float(v["low_stock_threshold"] or 5)
        except ValueError:
            messagebox.showerror("Invalid number", "Quantity, price and threshold must be numbers.", parent=self)
            return
        self.result = v
        self.destroy()


# ---------------------------------------------------------------------------
# Transaction dialog (use / restock)
# ---------------------------------------------------------------------------
class TxnDialog(tk.Toplevel):
    def __init__(self, parent, item_name, mode="use"):
        super().__init__(parent)
        self.title(("Use: " if mode == "use" else "Restock: ") + item_name)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text="Amount", bg=BG, fg=MUTED, font=FONT).grid(
            row=0, column=0, sticky="w", padx=18, pady=8)
        self._amount = tk.StringVar()
        e = ttk.Entry(self, textvariable=self._amount, width=18)
        e.grid(row=0, column=1, padx=(4, 18), pady=8)
        e.focus_set()

        tk.Label(self, text="Note", bg=BG, fg=MUTED, font=FONT).grid(
            row=1, column=0, sticky="w", padx=18, pady=8)
        self._note = tk.StringVar()
        ttk.Entry(self, textvariable=self._note, width=28).grid(
            row=1, column=1, padx=(4, 18), pady=8)

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(4, 14))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        style = "Red.TButton" if mode == "use" else "Green.TButton"
        label = "Record Usage" if mode == "use" else "Add Stock"
        ttk.Button(btn_frame, text=label, style=style, command=self._save).pack(side="left", padx=6)

        self.bind("<Return>", lambda _: self._save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()
        self.transient(parent)
        self.wait_window()

    def _save(self):
        try:
            amount = float(self._amount.get())
            if amount <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid", "Enter a positive number.", parent=self)
            return
        self.result = {"amount": amount, "note": self._note.get().strip()}
        self.destroy()


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Inventory Tracker")
        self.configure(bg=BG)
        self.geometry("1000x640")
        self.minsize(800, 500)
        _style()
        self._build()
        self.refresh_all()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        self._inv_tab   = self._build_inventory_tab(nb)
        self._usage_tab = self._build_usage_tab(nb)
        self._report_tab = self._build_report_tab(nb)

        nb.add(self._inv_tab,    text="  🗂  Inventory  ")
        nb.add(self._usage_tab,  text="  📋  Usage Log  ")
        nb.add(self._report_tab, text="  📊  Report     ")
        nb.bind("<<NotebookTabChanged>>", lambda _: self.refresh_all())

    # ------------------------------------------------------------------
    # Inventory tab
    # ------------------------------------------------------------------
    def _build_inventory_tab(self, parent):
        frame = ttk.Frame(parent)

        # Toolbar
        tb = tk.Frame(frame, bg=BG)
        tb.pack(fill="x", padx=14, pady=(12, 8))

        tk.Label(tb, text="Inventory", bg=BG, fg=TEXT, font=FONT_H).pack(side="left")

        btn_row = tk.Frame(tb, bg=BG)
        btn_row.pack(side="right")
        ttk.Button(btn_row, text="＋ Add Item", style="Accent.TButton",
                   command=self._add_item).pack(side="left", padx=4)
        ttk.Button(btn_row, text="↺ Refresh",
                   command=self.refresh_all).pack(side="left", padx=4)

        # Stats bar
        self._stats_frame = tk.Frame(frame, bg=SURFACE, pady=10)
        self._stats_frame.pack(fill="x", padx=14, pady=(0, 10))
        self._stat_labels = {}
        for key, label in [("skus","Total SKUs"), ("value","Inventory Value"),
                            ("low","Low Stock"), ("events","Usage Events")]:
            col = tk.Frame(self._stats_frame, bg=SURFACE)
            col.pack(side="left", padx=20)
            tk.Label(col, text=label, bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack()
            lbl = tk.Label(col, text="—", bg=SURFACE, fg=ACCENT, font=("Segoe UI", 16, "bold"))
            lbl.pack()
            self._stat_labels[key] = lbl

        # Treeview
        cols = ("Name", "Category", "Qty", "Unit", "Price", "Threshold", "Status")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        widths = (200, 110, 80, 70, 80, 90, 80)
        for col, w in zip(cols, widths):
            tv.heading(col, text=col)
            tv.column(col, width=w, anchor="w" if col in ("Name","Category") else "center")
        tv.tag_configure("low",    foreground=RED)
        tv.tag_configure("watch",  foreground=YELLOW)
        tv.tag_configure("ok",     foreground=GREEN)

        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=(0, 10))
        sb.pack(side="left", fill="y", padx=(0, 14), pady=(0, 10))
        self._inv_tree = tv

        # Action buttons below tree
        act = tk.Frame(frame, bg=BG)
        # (buttons inserted dynamically via right-click / double-click)
        tv.bind("<Double-1>", self._on_inv_double)
        tv.bind("<Button-3>", self._on_inv_right)

        return frame

    # ------------------------------------------------------------------
    # Usage tab
    # ------------------------------------------------------------------
    def _build_usage_tab(self, parent):
        frame = ttk.Frame(parent)

        tb = tk.Frame(frame, bg=BG)
        tb.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(tb, text="Usage Log", bg=BG, fg=TEXT, font=FONT_H).pack(side="left")

        # Filter controls
        fc = tk.Frame(tb, bg=BG)
        fc.pack(side="right")
        tk.Label(fc, text="Item:", bg=BG, fg=MUTED, font=FONT).pack(side="left", padx=(0, 4))
        self._usage_filter = ttk.Combobox(fc, width=18, state="readonly")
        self._usage_filter.pack(side="left", padx=(0, 10))
        self._usage_filter.bind("<<ComboboxSelected>>", lambda _: self._refresh_usage())
        tk.Label(fc, text="Limit:", bg=BG, fg=MUTED, font=FONT).pack(side="left", padx=(0, 4))
        self._usage_limit = ttk.Combobox(fc, values=["50", "100", "500"], width=6, state="readonly")
        self._usage_limit.set("50")
        self._usage_limit.pack(side="left")
        self._usage_limit.bind("<<ComboboxSelected>>", lambda _: self._refresh_usage())

        cols = ("Timestamp", "Item", "Type", "Amount", "Unit", "Note")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        widths = (160, 180, 80, 80, 70, 300)
        for col, w in zip(cols, widths):
            tv.heading(col, text=col)
            tv.column(col, width=w, anchor="w" if col in ("Item","Note","Timestamp") else "center")
        tv.tag_configure("use",     foreground=RED)
        tv.tag_configure("restock", foreground=GREEN)

        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=(0, 10))
        sb.pack(side="left", fill="y", padx=(0, 14), pady=(0, 10))
        self._usage_tree = tv
        return frame

    # ------------------------------------------------------------------
    # Report tab
    # ------------------------------------------------------------------
    def _build_report_tab(self, parent):
        frame = ttk.Frame(parent)
        tk.Label(frame, text="Report", bg=BG, fg=TEXT, font=FONT_H).pack(
            anchor="w", padx=14, pady=(12, 8))

        panes = tk.Frame(frame, bg=BG)
        panes.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        left  = tk.Frame(panes, bg=BG)
        right = tk.Frame(panes, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        tk.Label(left,  text="🔥 Top Consumed",  bg=BG, fg=MUTED, font=FONT_B).pack(anchor="w")
        tk.Label(right, text="📥 Top Restocked", bg=BG, fg=MUTED, font=FONT_B).pack(anchor="w")

        # Canvas for bar charts
        self._canvas_consumed  = tk.Canvas(left,  bg=SURFACE, highlightthickness=0, height=280)
        self._canvas_restocked = tk.Canvas(right, bg=SURFACE, highlightthickness=0, height=280)
        self._canvas_consumed.pack(fill="both", expand=True, pady=(6, 0))
        self._canvas_restocked.pack(fill="both", expand=True, pady=(6, 0))

        return frame

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------
    def refresh_all(self):
        self._refresh_inventory()
        self._refresh_usage()
        self._refresh_report()

    def _refresh_inventory(self):
        inv = load_inventory()
        usage = load_usage()

        # Stats
        total_value = sum(i["quantity"] * i["price"] for i in inv.values())
        low_count   = sum(1 for i in inv.values() if i["quantity"] <= i["low_stock_threshold"])
        self._stat_labels["skus"].config(text=str(len(inv)))
        self._stat_labels["value"].config(text=f"${total_value:,.2f}")
        self._stat_labels["low"].config(text=str(low_count),
                                        fg=RED if low_count else GREEN)
        self._stat_labels["events"].config(text=str(len(usage)))

        # Tree
        tv = self._inv_tree
        tv.delete(*tv.get_children())
        for item in sorted(inv.values(), key=lambda x: x["name"]):
            ratio = (item["quantity"] / item["low_stock_threshold"]
                     if item["low_stock_threshold"] > 0 else 2)
            if ratio < 1:
                tag, status = "low", "Low Stock"
            elif ratio < 1.5:
                tag, status = "watch", "Watch"
            else:
                tag, status = "ok", "OK"
            price = f"${item['price']:.2f}" if item["price"] else "—"
            tv.insert("", "end", iid=item["name"], tags=(tag,), values=(
                item["name"],
                item["category"],
                f"{item['quantity']:.2f}",
                item["unit"],
                price,
                item["low_stock_threshold"],
                status,
            ))

    def _refresh_usage(self):
        inv     = load_inventory()
        all_usage = load_usage()

        # Update combobox options
        names = ["All items"] + sorted(i["name"] for i in inv.values())
        self._usage_filter["values"] = names
        if not self._usage_filter.get():
            self._usage_filter.set("All items")

        selected = self._usage_filter.get()
        limit    = int(self._usage_limit.get() or 50)

        entries = list(reversed(all_usage))
        if selected and selected != "All items":
            key = selected.lower().strip()
            entries = [e for e in entries if e["item_key"] == key]
        entries = entries[:limit]

        tv = self._usage_tree
        tv.delete(*tv.get_children())
        for e in entries:
            ts       = e["timestamp"][:19].replace("T", " ")
            is_restock = e["amount"] < 0
            kind     = "Restock" if is_restock else "Use"
            amount   = f"+{abs(e['amount']):.2f}" if is_restock else f"-{e['amount']:.2f}"
            tag      = "restock" if is_restock else "use"
            tv.insert("", "end", tags=(tag,), values=(
                ts, e["item_name"], kind, amount, e["unit"], e.get("note", "")
            ))

    def _refresh_report(self):
        inv   = load_inventory()
        usage = load_usage()

        consumed: dict  = {}
        restocked: dict = {}
        for e in usage:
            key = e["item_key"]
            if e["amount"] < 0:
                restocked[key] = restocked.get(key, 0) + abs(e["amount"])
            else:
                consumed[key]  = consumed.get(key, 0) + e["amount"]

        def name_of(key):
            return inv.get(key, {}).get("name", key)
        def unit_of(key):
            return inv.get(key, {}).get("unit", "")

        top_c = sorted(consumed.items(),  key=lambda x: x[1], reverse=True)[:10]
        top_r = sorted(restocked.items(), key=lambda x: x[1], reverse=True)[:10]

        self._draw_chart(self._canvas_consumed,  top_c, name_of, unit_of, RED)
        self._draw_chart(self._canvas_restocked, top_r, name_of, unit_of, GREEN)

    def _draw_chart(self, canvas, data, name_of, unit_of, color):
        canvas.delete("all")
        if not data:
            canvas.create_text(10, 20, anchor="w", text="No data yet.",
                                fill=MUTED, font=FONT)
            return
        canvas.update_idletasks()
        W = canvas.winfo_width() or 400
        max_val = data[0][1]
        bar_h, gap, left_margin, right_margin = 18, 8, 150, 70
        for i, (key, val) in enumerate(data):
            y = 14 + i * (bar_h + gap)
            label = name_of(key)[:22]
            canvas.create_text(left_margin - 6, y + bar_h // 2, anchor="e",
                                text=label, fill=TEXT, font=("Segoe UI", 9))
            track_w = W - left_margin - right_margin
            canvas.create_rectangle(left_margin, y, left_margin + track_w, y + bar_h,
                                     fill=SURFACE2, outline="")
            bar_w = max(4, int(track_w * val / max_val))
            canvas.create_rectangle(left_margin, y, left_margin + bar_w, y + bar_h,
                                     fill=color, outline="")
            canvas.create_text(left_margin + track_w + 6, y + bar_h // 2, anchor="w",
                                text=f"{val:.1f} {unit_of(key)}", fill=MUTED,
                                font=("Segoe UI", 9))

    # ------------------------------------------------------------------
    # Inventory actions
    # ------------------------------------------------------------------
    def _add_item(self):
        dlg = ItemDialog(self, title="Add Item")
        if dlg.result:
            d = dlg.result
            add_item(d["name"], d["quantity"], d["unit"],
                     d["category"], d["low_stock_threshold"], d["price"])
            self.refresh_all()

    def _edit_item(self, name):
        inv  = load_inventory()
        item = inv.get(name.lower().strip())
        if not item:
            return
        dlg = ItemDialog(self, title=f"Edit: {name}", data=item)
        if dlg.result:
            d = dlg.result
            update_item(name, quantity=d["quantity"], unit=d["unit"],
                        category=d["category"],
                        low_stock_threshold=d["low_stock_threshold"],
                        price=d["price"])
            self.refresh_all()

    def _use_item(self, name):
        dlg = TxnDialog(self, name, mode="use")
        if dlg.result:
            record_usage(name, dlg.result["amount"], dlg.result["note"])
            self.refresh_all()

    def _restock_item(self, name):
        dlg = TxnDialog(self, name, mode="restock")
        if dlg.result:
            restock(name, dlg.result["amount"], dlg.result["note"])
            self.refresh_all()

    def _delete_item(self, name):
        if messagebox.askyesno("Confirm", f"Delete '{name}' from inventory?"):
            remove_item(name)
            self.refresh_all()

    def _selected_name(self):
        sel = self._inv_tree.selection()
        return sel[0] if sel else None

    def _on_inv_double(self, _event):
        name = self._selected_name()
        if name:
            self._edit_item(name)

    def _on_inv_right(self, event):
        name = self._selected_name()
        if not name:
            # select row under cursor
            row = self._inv_tree.identify_row(event.y)
            if row:
                self._inv_tree.selection_set(row)
                name = row
        if not name:
            return
        menu = tk.Menu(self, tearoff=0, bg=SURFACE2, fg=TEXT,
                       activebackground=BORDER, activeforeground=TEXT,
                       bd=0, font=FONT)
        menu.add_command(label="▼  Record Usage",  command=lambda: self._use_item(name))
        menu.add_command(label="▲  Restock",        command=lambda: self._restock_item(name))
        menu.add_separator()
        menu.add_command(label="✎  Edit",           command=lambda: self._edit_item(name))
        menu.add_command(label="✕  Delete",         command=lambda: self._delete_item(name))
        menu.tk_popup(event.x_root, event.y_root)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    App().mainloop()
