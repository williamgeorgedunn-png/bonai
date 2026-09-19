import itertools


class MemoryItem:
    __slots__ = ("id", "text", "pinned", "source", "step", "tokens")

    def __init__(self, id, text, pinned=False, source="", step="", tokens=0):
        self.id = id
        self.text = text
        self.pinned = bool(pinned)
        self.source = source
        self.step = step
        self.tokens = tokens

    def to_dict(self):
        data = dict(id=self.id, text=self.text)
        if self.pinned:
            data["pinned"] = True
        if self.source:
            data["source"] = self.source
        if self.step:
            data["step"] = self.step
        return data


class WorkingMemory:
    """A token-capped set of facts the architect chose to keep.

    Facts retrieved for a step are discarded when the step ends. Only items
    the architect explicitly asked to remember land here, and the cap is
    enforced by eviction, so architect context cannot grow without bound.
    """

    def __init__(self, max_tokens=2000, token_count=None):
        self.max_tokens = max_tokens
        self.items = []
        self.evicted = 0
        self._counter = itertools.count(1)
        self.token_count = token_count or (lambda text: max(1, len(text) // 4))

    # ------------------------------------------------------------------ io

    def load(self, raw_items):
        self.items = []
        for raw in raw_items or []:
            if not isinstance(raw, dict) or not raw.get("text"):
                continue
            item = MemoryItem(
                id=str(raw.get("id") or self._new_id()),
                text=str(raw["text"]),
                pinned=raw.get("pinned", False),
                source=raw.get("source", ""),
                step=raw.get("step", ""),
            )
            item.tokens = self.token_count(item.text)
            self.items.append(item)
        self._reseed_counter()

    def dump(self):
        return [item.to_dict() for item in self.items]

    def _new_id(self):
        return f"M{next(self._counter)}"

    def _reseed_counter(self):
        highest = 0
        for item in self.items:
            if item.id.startswith("M") and item.id[1:].isdigit():
                highest = max(highest, int(item.id[1:]))
        self._counter = itertools.count(highest + 1)

    # --------------------------------------------------------------- edits

    def get(self, item_id):
        for item in self.items:
            if item.id.lower() == str(item_id).lower():
                return item
        return None

    def add(self, text, pinned=False, source="", step=""):
        text = " ".join(str(text).split())
        if not text:
            return None
        for existing in self.items:
            if existing.text == text:
                existing.pinned = existing.pinned or pinned
                return existing
        item = MemoryItem(
            id=self._new_id(),
            text=text,
            pinned=pinned,
            source=source,
            step=step,
            tokens=self.token_count(text),
        )
        self.items.append(item)
        return item

    def forget(self, item_id):
        item = self.get(item_id)
        if item is None:
            return False
        self.items.remove(item)
        return True

    def rewrite(self, item_id, text):
        item = self.get(item_id)
        if item is None:
            return False
        item.text = " ".join(str(text).split())
        item.tokens = self.token_count(item.text)
        return True

    def keep_only(self, keep_ids):
        keep = {str(i).lower() for i in keep_ids}
        self.items = [i for i in self.items if i.id.lower() in keep or i.pinned]

    # -------------------------------------------------------------- budget

    def total_tokens(self):
        return sum(item.tokens for item in self.items)

    def over_budget(self):
        return self.total_tokens() > self.max_tokens

    def evict_to_fit(self):
        """Drop unpinned items, oldest first, until inside the cap.

        Returns the ids that were dropped.
        """
        dropped = []
        while self.over_budget():
            victim = next((i for i in self.items if not i.pinned), None)
            if victim is None:
                break
            self.items.remove(victim)
            dropped.append(victim.id)
            self.evicted += 1
        return dropped

    def pinned_overflow(self):
        """True when pinned items alone no longer fit."""
        return sum(i.tokens for i in self.items if i.pinned) > self.max_tokens

    # -------------------------------------------------------------- render

    def render(self):
        if not self.items:
            return "(empty)"
        # Pinned first, then newest last-added first, so the tail is droppable.
        ordered = [i for i in self.items if i.pinned] + [
            i for i in reversed(self.items) if not i.pinned
        ]
        lines = []
        for item in ordered:
            flag = " PINNED" if item.pinned else ""
            lines.append(f"[{item.id}{flag}, ~{item.tokens} tokens] {item.text}")
        used = self.total_tokens()
        lines.append(f"(using ~{used} of {self.max_tokens} tokens)")
        return "\n".join(lines)
