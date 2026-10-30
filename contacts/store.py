"""Local contact book with exact name matching and strict privacy guarantees.

Stores up to 200 contacts in an isolated SQLite database with normalized unique
display names and aliases. Contact write operations (add, update, delete) are
restricted solely to the UI window and terminal interactive commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Optional, Union

from config.paths import get_contacts_db_path

MAX_CONTACTS: int = 200

# Name characters: letters, digits, space, apostrophe, hyphen
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9 '\-]+$")

# Phone digits: 8-15 digits, no leading zero
_PHONE_PATTERN = re.compile(r"^[1-9][0-9]{7,14}$")

# Email pattern: simple validation, max 120 characters
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactError(Exception):
    """Base exception for contact store operations."""
    pass


class ContactValidationError(ContactError, ValueError):
    """Raised when contact field validation fails."""
    pass


class ContactLimit(ContactValidationError):
    """Raised when contact storage capacity limit (200) is reached."""
    pass


class ContactNotFoundError(ContactError, KeyError):
    """Raised when looking up an unknown contact."""
    pass


class AmbiguousContactError(ContactError, ValueError):
    """Raised when a query matches multiple ambiguous records."""
    pass


def normalize_name(s: str) -> str:
    """Normalize a name: lowercase and collapsed whitespace."""
    if not s or not isinstance(s, str):
        return ""
    return " ".join(s.strip().split()).lower()


def mask_phone(digits: Optional[str]) -> str:
    """Mask phone number showing only the last 4 digits (e.g. '••••1234')."""
    if not digits:
        return ""
    clean = str(digits).strip()
    return f"••••{clean[-4:]}"

@dataclass(frozen=True)
class ContactRef:
    """Opaque contact reference used by the messaging layer.

    Security guarantee:
    repr and str strictly NEVER contain the phone number or email address,
    preventing unintended credential/PII leaks into model context or logs.
    """

    id: int
    display_name: str
    phone: Optional[str] = field(default=None, repr=False)
    email: Optional[str] = field(default=None, repr=False)
    voice_approvable: bool = field(default=False, repr=False)

    def __repr__(self) -> str:
        return f"ContactRef(id={self.id}, display_name={self.display_name!r})"

    def __str__(self) -> str:
        return self.display_name


@dataclass
class Contact:
    """Full contact record stored in the local SQLite database."""

    id: int
    display_name: str
    aliases: list[str] = field(default_factory=list)
    phone: Optional[str] = None
    email: Optional[str] = None
    voice_approvable: bool = False
    created_at: float = field(default_factory=time.time)

    def to_ref(self) -> ContactRef:
        """Create an opaque ContactRef for the messaging service."""
        return ContactRef(
            id=self.id,
            display_name=self.display_name,
            phone=self.phone,
            email=self.email,
            voice_approvable=self.voice_approvable,
        )


class ContactStore:
    """Thread-safe SQLite contact store with parameterized SQL and corruption resilience."""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
    ) -> None:
        target = path if path is not None else db_path
        self.db_path = Path(target) if target is not None else get_contacts_db_path()
        self._ensure_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Open a new connection per operation."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        """Initialize schema, recreating the database if corrupt."""
        conn = None
        try:
            conn = self._get_connection()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    phone TEXT,
                    email TEXT,
                    voice_approvable INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._recreate_db()

    def _recreate_db(self) -> None:
        """Remove corrupt database file and create a fresh schema."""
        import gc
        gc.collect()
        for p in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        conn = self._get_connection()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    phone TEXT,
                    email TEXT,
                    voice_approvable INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _validate_display_name(name: str) -> str:
        """Validate display name: 1-40 chars, letters/digits/space/apostrophe/hyphen."""
        if not name or not isinstance(name, str):
            raise ContactValidationError("Contact name cannot be empty.")
        clean = " ".join(name.strip().split())
        if not (1 <= len(clean) <= 40):
            raise ContactValidationError(
                f"Contact name must be between 1 and 40 characters (got {len(clean)})."
            )
        if not _NAME_PATTERN.match(clean) or not any(c.isalnum() for c in clean):
            raise ContactValidationError(
                "Contact name may only contain letters, digits, spaces, apostrophes, and hyphens."
            )
        return clean

    @classmethod
    def _validate_aliases(cls, aliases: Optional[list[str]], display_name: str) -> list[str]:
        """Validate aliases: up to 5 aliases, each 1-30 chars, no duplicates."""
        if not aliases:
            return []
        if len(aliases) > 5:
            raise ContactValidationError(
                f"Contact can have at most 5 aliases (got {len(aliases)})."
            )
        norm_display = normalize_name(display_name)
        seen_norms: set[str] = set()
        clean_aliases: list[str] = []

        for alias in aliases:
            if not alias or not isinstance(alias, str):
                continue
            clean = " ".join(alias.strip().split())
            if not (1 <= len(clean) <= 30):
                raise ContactValidationError(
                    f"Alias must be between 1 and 30 characters (got {len(clean)})."
                )
            if not _NAME_PATTERN.match(clean) or not any(c.isalnum() for c in clean):
                raise ContactValidationError(
                    "Alias may only contain letters, digits, spaces, apostrophes, and hyphens."
                )
            norm = normalize_name(clean)
            if norm == norm_display:
                raise ContactValidationError(
                    f"Alias '{clean}' cannot be identical to the contact's display name."
                )
            if norm in seen_norms:
                raise ContactValidationError(
                    f"Duplicate alias '{clean}' specified for contact."
                )
            seen_norms.add(norm)
            clean_aliases.append(clean)

        return clean_aliases

    @staticmethod
    def _validate_phone(phone: Optional[str]) -> Optional[str]:
        """Validate phone number: digits only, 8-15 digits, no leading zero."""
        if phone is None:
            return None
        clean = str(phone).strip()
        if not clean:
            return None
        if not _PHONE_PATTERN.match(clean):
            raise ContactValidationError(
                f"Phone number must be 8-15 digits with no leading zero (got '{clean}')."
            )
        return clean

    @staticmethod
    def _validate_email(email: Optional[str]) -> Optional[str]:
        """Validate email address: simple validation, max 120 characters."""
        if email is None:
            return None
        clean = str(email).strip()
        if not clean:
            return None
        if len(clean) > 120 or not _EMAIL_PATTERN.match(clean):
            raise ContactValidationError(
                f"Invalid email address (max 120 characters, got '{clean}')."
            )
        return clean

    def _check_name_uniqueness(
        self,
        conn: sqlite3.Connection,
        display_name: str,
        aliases: list[str],
        exclude_id: Optional[int] = None,
    ) -> None:
        """Ensure all proposed names and aliases are globally unique across all contacts."""
        norm_to_check = {normalize_name(display_name): display_name}
        for a in aliases:
            norm_to_check[normalize_name(a)] = a

        query = "SELECT id, display_name, aliases FROM contacts"
        params: tuple = ()
        if exclude_id is not None:
            query += " WHERE id != ?"
            params = (exclude_id,)

        cursor = conn.execute(query, params)
        for row in cursor.fetchall():
            row_display = row["display_name"]
            row_aliases = json.loads(row["aliases"] or "[]")

            # Check display name
            norm_row_name = normalize_name(row_display)
            if norm_row_name in norm_to_check:
                conflicting = norm_to_check[norm_row_name]
                raise ContactValidationError(
                    f"Name or alias '{conflicting}' conflicts with existing contact '{row_display}'."
                )

            # Check aliases
            for existing_alias in row_aliases:
                norm_existing_alias = normalize_name(existing_alias)
                if norm_existing_alias in norm_to_check:
                    conflicting = norm_to_check[norm_existing_alias]
                    raise ContactValidationError(
                        f"Name or alias '{conflicting}' conflicts with existing alias of '{row_display}'."
                    )

    def count(self) -> int:
        """Return total number of saved contacts."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM contacts")
                return int(cursor.fetchone()[0])
        except sqlite3.DatabaseError:
            self._recreate_db()
            return 0

    def add(
        self,
        display_name: str,
        aliases: Optional[list[str]] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        voice_approvable: bool = False,
    ) -> Contact:
        """Add a new contact to the store."""
        clean_name = self._validate_display_name(display_name)
        clean_aliases = self._validate_aliases(aliases, clean_name)
        clean_phone = self._validate_phone(phone)
        clean_email = self._validate_email(email)

        now = time.time()
        aliases_json = json.dumps(clean_aliases)

        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM contacts")
                curr_count = int(cursor.fetchone()[0])
                if curr_count >= MAX_CONTACTS:
                    raise ContactLimit(
                        f"Contact limit reached (max {MAX_CONTACTS})."
                    )

                self._check_name_uniqueness(conn, clean_name, clean_aliases)

                cur = conn.execute(
                    """
                    INSERT INTO contacts (display_name, aliases, phone, email, voice_approvable, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (clean_name, aliases_json, clean_phone, clean_email, int(voice_approvable), now),
                )
                conn.commit()
                contact_id = cur.lastrowid
                return Contact(
                    id=contact_id,
                    display_name=clean_name,
                    aliases=clean_aliases,
                    phone=clean_phone,
                    email=clean_email,
                    voice_approvable=bool(voice_approvable),
                    created_at=now,
                )
        except sqlite3.DatabaseError:
            self._recreate_db()
            raise

    def update(
        self,
        contact_id: int,
        display_name: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        voice_approvable: Optional[bool] = None,
    ) -> Contact:
        """Update an existing contact by id."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, display_name, aliases, phone, email, voice_approvable, created_at FROM contacts WHERE id = ?",
                    (contact_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ContactNotFoundError(f"Contact id {contact_id} not found.")

                curr_display = row["display_name"]
                curr_aliases = json.loads(row["aliases"] or "[]")
                curr_phone = row["phone"]
                curr_email = row["email"]
                curr_voice = bool(row["voice_approvable"])
                created_at = float(row["created_at"])

                new_name = self._validate_display_name(display_name) if display_name is not None else curr_display
                new_aliases = (
                    self._validate_aliases(aliases, new_name)
                    if aliases is not None
                    else curr_aliases
                )
                new_phone = self._validate_phone(phone) if phone is not None else curr_phone
                new_email = self._validate_email(email) if email is not None else curr_email
                new_voice = bool(voice_approvable) if voice_approvable is not None else curr_voice

                self._check_name_uniqueness(conn, new_name, new_aliases, exclude_id=contact_id)

                conn.execute(
                    """
                    UPDATE contacts
                    SET display_name = ?, aliases = ?, phone = ?, email = ?, voice_approvable = ?
                    WHERE id = ?
                    """,
                    (new_name, json.dumps(new_aliases), new_phone, new_email, int(new_voice), contact_id),
                )
                conn.commit()

                return Contact(
                    id=contact_id,
                    display_name=new_name,
                    aliases=new_aliases,
                    phone=new_phone,
                    email=new_email,
                    voice_approvable=new_voice,
                    created_at=created_at,
                )
        except sqlite3.DatabaseError:
            self._recreate_db()
            raise

    def delete(self, contact_id: int) -> bool:
        """Delete a contact by id."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.DatabaseError:
            self._recreate_db()
            return False

    def get(self, contact_id: int) -> Optional[Contact]:
        """Fetch a single contact by id."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, display_name, aliases, phone, email, voice_approvable, created_at FROM contacts WHERE id = ?",
                    (contact_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return Contact(
                    id=row["id"],
                    display_name=row["display_name"],
                    aliases=json.loads(row["aliases"] or "[]"),
                    phone=row["phone"],
                    email=row["email"],
                    voice_approvable=bool(row["voice_approvable"]),
                    created_at=float(row["created_at"]),
                )
        except sqlite3.DatabaseError:
            self._recreate_db()
            return None

    def list_contacts(self) -> list[Contact]:
        """Return all contacts ordered by display name."""
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, display_name, aliases, phone, email, voice_approvable, created_at FROM contacts ORDER BY display_name ASC"
                )
                contacts: list[Contact] = []
                for row in cursor.fetchall():
                    contacts.append(
                        Contact(
                            id=row["id"],
                            display_name=row["display_name"],
                            aliases=json.loads(row["aliases"] or "[]"),
                            phone=row["phone"],
                            email=row["email"],
                            voice_approvable=bool(row["voice_approvable"]),
                            created_at=float(row["created_at"]),
                        )
                    )
                return contacts
        except sqlite3.DatabaseError:
            self._recreate_db()
            return []

    def list_names(self) -> list[str]:
        """Return all unique contact display names and aliases (names only)."""
        names: list[str] = []
        for contact in self.list_contacts():
            names.append(contact.display_name)
            names.extend(contact.aliases)
        return names

    def resolve(self, name: str) -> ContactRef:
        """Resolve a name or alias strictly by exact normalized match (never fuzzy).

        Returns:
            ContactRef (opaque reference without phone or email in repr/str)

        Raises:
            ContactNotFoundError: If no contact matches the name or alias.
            AmbiguousContactError: If multiple contacts match (ambiguous).
        """
        if not name or not isinstance(name, str):
            raise ContactNotFoundError("Empty contact name.")

        query_norm = normalize_name(name)
        if not query_norm:
            raise ContactNotFoundError("Empty contact name.")

        matches: list[Contact] = []
        for contact in self.list_contacts():
            if normalize_name(contact.display_name) == query_norm:
                matches.append(contact)
                continue
            for alias in contact.aliases:
                if normalize_name(alias) == query_norm:
                    matches.append(contact)
                    break

        if len(matches) == 0:
            raise ContactNotFoundError(f"Contact '{name}' not found.")
        if len(matches) > 1:
            raise AmbiguousContactError(f"Ambiguous contact '{name}' matches multiple records.")

        return matches[0].to_ref()

