"""Small Tkinter help popups, with no extra dependencies."""
import tkinter as tk


class Tooltip:
    def __init__(self, widget, text, delay_ms=350):
        self.widget, self.text, self.delay_ms = widget, text, delay_ms
        self.pending = self.window = None
        for event in ("<Enter>", "<FocusIn>"):
            widget.bind(event, self.schedule, add="+")
        for event in ("<Leave>", "<FocusOut>", "<ButtonPress>", "<Escape>", "<Unmap>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.pending = self.widget.after(self.delay_ms, self.show)

    def show(self):
        self.pending = None
        if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
            return
        window = self.window = tk.Toplevel(self.widget)
        window.withdraw()
        window.overrideredirect(True)
        window.transient(self.widget.winfo_toplevel())
        tk.Label(window, text=self.text, justify="left", wraplength=330,
                 background="#FFF5F8", foreground="#422C36", relief="solid",
                 borderwidth=1, padx=10, pady=8, font=("Segoe UI", 10)).pack()
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth()-width-8)
        y = self.widget.winfo_rooty()+self.widget.winfo_height()+6
        if y+height > self.widget.winfo_screenheight()-8:
            y = self.widget.winfo_rooty()-height-6
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()

    def hide(self, _event=None):
        if self.pending is not None:
            try:
                self.widget.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None
        if self.window is not None:
            window, self.window = self.window, None
            try:
                window.destroy()
            except tk.TclError:
                pass
