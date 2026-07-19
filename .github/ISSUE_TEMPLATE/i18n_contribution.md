name: i18n contribution
about: Add or improve translations
title: "[I18N] "
labels: i18n, help wanted
assignees: ''

---

**Which language?**
- [ ] 中文 (zh) — already supported
- [ ] English (en) — already supported
- [ ] 日本語 (ja)
- [ ] 한국어 (ko)
- [ ] Español (es)
- [ ] Français (fr)
- [ ] Deutsch (de)
- [ ] Other: ___

**Which strings?**

List the `msg_id` keys you'd translate. See `i18n_messages.py` for the full list.

**Acceptance criteria**
- [ ] Added 2-letter code: {zh, en, ja, ...}
- [ ] Format placeholders preserved (`{count}`, `{pct:.0f}`)
- [ ] Smoke tested with `MNELO_MEMORY_LANG=<code>`
