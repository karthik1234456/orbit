"""Contacts management window for ORBIT.

Allows viewing, adding, editing, and deleting local contacts with masked phone display.
Opens from SettingsWindow and the Command Palette ("Contacts").
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Optional

from contacts.store import (
    Contact,
    ContactStore,
    ContactValidationError,
    mask_phone,
)
from ui.theme import Palette, apply_theme, get_palette


class ContactEditorDialog(tk.Toplevel):
    """Modal dialog for adding or editing a contact record."""

    def __init__(
        self,
        parent: tk.Widget,
        store: ContactStore,
        contact: Optional[Contact] = None,
        on_saved: Optional[Any] = None,
        palette: Optional[Palette] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.contact = contact
        self.on_saved = on_saved
        self.palette = palette or getattr(parent, "palette", None) or get_palette()

        mode = "Edit" if contact else "Add"
        self.title(f"{mode} Contact")
        self.transient(parent)
        self.geometry("460x380")
        self.minsize(400, 340)
        self.resizable(False, False)

        apply_theme(self, self.palette)
        self._build_ui()
        self.grab_set()

    def _build_ui(self) -> None:
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)

        title_txt = f"{'Edit' if self.contact else 'Add'} Contact"
        lbl_head = ttk.Label(f, text=title_txt, font=("Segoe UI", 12, "bold"))
        lbl_head.pack(anchor="w", pady=(0, 12))

        # Display Name
        ttk.Label(f, text="Display Name:").pack(anchor="w")
        self.name_var = tk.StringVar(value=self.contact.display_name if self.contact else "")
        self.name_entry = ttk.Entry(f, textvariable=self.name_var)
        self.name_entry.pack(fill="x", pady=(2, 8))

        # Phone Number (full digits shown in edit form)
        ttk.Label(f, text="Phone Number (8-15 digits, digits only, optional):").pack(anchor="w")
        self.phone_var = tk.StringVar(value=self.contact.phone if self.contact else "")
        self.phone_entry = ttk.Entry(f, textvariable=self.phone_var)
        self.phone_entry.pack(fill="x", pady=(2, 8))

        # Email
        ttk.Label(f, text="Email Address (optional):").pack(anchor="w")
        self.email_var = tk.StringVar(value=self.contact.email if self.contact else "")
        self.email_entry = ttk.Entry(f, textvariable=self.email_var)
        self.email_entry.pack(fill="x", pady=(2, 8))

        # Aliases
        ttk.Label(f, text="Aliases (comma-separated, max 5, optional):").pack(anchor="w")
        aliases_str = ", ".join(self.contact.aliases) if self.contact and self.contact.aliases else ""
        self.aliases_var = tk.StringVar(value=aliases_str)
        self.aliases_entry = ttk.Entry(f, textvariable=self.aliases_var)
        self.aliases_entry.pack(fill="x", pady=(2, 14))

        # Error label
        self.err_lbl = ttk.Label(f, text="", foreground="#ef4444", wraplength=400)
        self.err_lbl.pack(fill="x", pady=(0, 8))

        # Action buttons
        btn_box = ttk.Frame(f)
        btn_box.pack(fill="x", side="bottom")

        cancel_btn = ttk.Button(btn_box, text="Cancel", command=self.destroy)
        cancel_btn.pack(side="right", padx=(8, 0))

        save_btn = ttk.Button(btn_box, text="Save", command=self._on_save)
        save_btn.pack(side="right")

        self.name_entry.focus_set()

    def _on_save(self) -> None:
        name = self.name_var.get().strip()
        phone = self.phone_var.get().strip() or None
        email = self.email_var.get().strip() or None
        raw_aliases = self.aliases_var.get()
        aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()]

        try:
            if self.contact:
                self.store.update(
                    self.contact.id,
                    display_name=name,
                    phone=phone,
                    email=email,
                    aliases=aliases,
                )
            else:
                self.store.add(
                    display_name=name,
                    phone=phone,
                    email=email,
                    aliases=aliases,
                )
            if self.on_saved:
                self.on_saved()
            self.destroy()
        except ContactValidationError as err:
            self.err_lbl.config(text=str(err))
        except Exception as exc:
            self.err_lbl.config(text=f"Failed to save contact: {exc}")


class ContactsWindow(tk.Toplevel):
    """Main window for viewing and managing saved contacts."""

    def __init__(
        self,
        parent: tk.Widget,
        contacts: Optional[ContactStore] = None,
        controller: Optional[Any] = None,
        palette: Optional[Palette] = None,
    ) -> None:
        super().__init__(parent)
        self.parent_root = parent
        self.controller = controller
        self.palette = palette or getattr(parent, "palette", None) or get_palette()

        if contacts is not None:
            self.store = contacts
        else:
            c_store = getattr(controller, "contact_store", None)
            if c_store is not None:
                self.store = c_store
            else:
                self.store = ContactStore()

        self.title("ORBIT Contacts")
        self.transient(parent)
        self.geometry("700x520")
        self.minsize(560, 400)

        apply_theme(self, self.palette)
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self, padding=12)
        main_frame.pack(fill="both", expand=True)

        header = ttk.Label(main_frame, text="Local Contact Book", font=("Segoe UI", 12, "bold"))
        header.pack(anchor="w", pady=(0, 4))

        sub = ttk.Label(
            main_frame,
            text="Contacts are stored locally. Phone numbers are masked in lists for privacy.",
            font=("Segoe UI", 9, "italic"),
            foreground="#4b5563",
        )
        sub.pack(anchor="w", pady=(0, 10))

        # Table frame
        tbl_frame = ttk.Frame(main_frame)
        tbl_frame.pack(fill="both", expand=True)

        cols = ("id", "name", "phone", "email", "aliases")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", selectmode="browse")

        self.tree.heading("id", text="#")
        self.tree.heading("name", text="Display Name")
        self.tree.heading("phone", text="Phone Number")
        self.tree.heading("email", text="Email")
        self.tree.heading("aliases", text="Aliases")

        self.tree.column("id", width=40, anchor="center")
        self.tree.column("name", width=150, anchor="w")
        self.tree.column("phone", width=120, anchor="w")
        self.tree.column("email", width=160, anchor="w")
        self.tree.column("aliases", width=160, anchor="w")

        scrollbar = ttk.Scrollbar(tbl_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self._on_edit())

        # Action button bar
        btn_bar = ttk.Frame(main_frame, padding=(0, 12, 0, 0))
        btn_bar.pack(fill="x", side="bottom")

        close_btn = ttk.Button(btn_bar, text="Close", command=self.destroy)
        close_btn.pack(side="right", padx=(6, 0))

        del_btn = ttk.Button(btn_bar, text="Delete Contact", command=self._on_delete)
        del_btn.pack(side="right", padx=(6, 0))

        edit_btn = ttk.Button(btn_bar, text="Edit Contact", command=self._on_edit)
        edit_btn.pack(side="right", padx=(6, 0))

        add_btn = ttk.Button(btn_bar, text="Add Contact", command=self._on_add)
        add_btn.pack(side="right")

    def _refresh(self) -> None:
        """Reload contacts into the treeview."""
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            contacts = self.store.list_contacts()
            for c in contacts:
                masked = mask_phone(c.phone) if c.phone else ""
                aliases_str = ", ".join(c.aliases) if c.aliases else ""
                self.tree.insert(
                    "",
                    "end",
                    iid=str(c.id),
                    values=(c.id, c.display_name, masked, c.email or "", aliases_str),
                )
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to load contacts: {exc}", parent=self)

    def _get_selected_contact(self) -> Optional[Contact]:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Selection Required", "Please select a contact from the list.", parent=self)
            return None
        contact_id = int(sel[0])
        return self.store.get(contact_id)

    def _on_add(self) -> None:
        ContactEditorDialog(self, store=self.store, on_saved=self._refresh, palette=self.palette)

    def _on_edit(self) -> None:
        contact = self._get_selected_contact()
        if contact is not None:
            ContactEditorDialog(
                self,
                store=self.store,
                contact=contact,
                on_saved=self._refresh,
                palette=self.palette,
            )

    def _on_delete(self) -> None:
        contact = self._get_selected_contact()
        if contact is None:
            return

        confirmed = messagebox.askyesno(
            "Delete Contact",
            f"Delete contact '{contact.display_name}'?",
            parent=self,
        )
        if confirmed:
            try:
                self.store.delete(contact.id)
                self._refresh()
            except Exception as exc:
                messagebox.showerror("Delete Error", f"Failed to delete contact: {exc}", parent=self)

