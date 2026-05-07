---
name: book-to-skill
description: Converts a technical book (PDF or EPUB) into a structured Claude Code skill — extracting frameworks, mental models, principles, techniques, and anti-patterns the author crystallized. Use when the user wants to study a book through Claude, apply an author's frameworks while working, or build a reusable knowledge base from any PDF or EPUB.
when_to_use: Trigger phrases — "turn this book into a skill", "create a skill from this PDF", "create a skill from this EPUB", "I want to study X book", "add this book to my skills", "convert PDF to skill", "convert EPUB to skill", "analyze this book", "extract frameworks from this book". Accepts a path to a PDF or EPUB and optional skill name slug.
disable-model-invocation: true
context: fork
agent: general-purpose
allowed-tools: Bash(python3 *) Bash(pdftotext *) Bash(mkdir *) Bash(cp *) Bash(find *) Bash(wc *) Bash(echo *) Bash(cat *) Bash(date *) Bash(ls *) Bash(file *) Read Write Glob Grep Task
argument-hint: <path-to-pdf-or-epub> [skill-name-slug]
arguments: [book_path, skill_name]
effort: high
---

# Book-to-Skill Converter

Transform written knowledge into actionable Claude Code skills by extracting structure — not producing summaries.

## Philosophy

Books contain crystallized expertise: frameworks, principles, and techniques that took years to develop. This skill extracts that knowledge into a format Claude can leverage repeatedly.

**Extract structure, not summaries.** A skill isn't a book report. It's a toolkit of:
- Named frameworks (mental models with clear application)
- Actionable principles (rules that guide decisions)
- Techniques (step-by-step methods)
- Anti-patterns (what to avoid and why)
- Voice calibration (how the author thinks and communicates)

**Preserve the author's precision.** Frameworks often have specific names for reasons. "The 5 Whys" isn't interchangeable with "ask why multiple times." Capture the exact formulation.

**Layer depth appropriately.** Simple books → simple skills. Complex books with 10+ frameworks → skills with reference files and on-demand chapters.

---

## Modes of Operation

Three paths available. Route based on what the user asks:

### 1. Full Conversion (Default)
**Trigger:** User provides a PDF path without special instructions
**Action:** Run all steps below (Steps 0–9)
**Output:** Complete skill with SKILL.md, chapters/, glossary, patterns, cheatsheet

### 2. Analyze Only
**Trigger:** User says "analyze", "just extract", or "I want to review before generating"
**Action:** Run Steps 0–3, then produce a structured extraction report (frameworks, principles, techniques found). Stop — do NOT generate skill files.
**Output:** Analysis report for user review

### 3. Generate from Prior Analysis
**Trigger:** User has existing analysis notes or previously ran analyze-only
**Action:** Skip Steps 0–3, use the provided analysis as input, run Steps 4–9
**Output:** Skill files from the provided analysis

---

## Step 0 — Out-of-scope check

If the argument is NOT a path to a PDF or EPUB file, stop and respond:
> "book-to-skill requires a PDF or EPUB path. Usage: `/book-to-skill /path/to/book.pdf [skill-name]` or `/book-to-skill /path/to/book.epub [skill-name]`"

---

## Step 1 — Validate input

```bash
BOOK_PATH="$1"
SKILL_NAME_ARG="${2:-}"
test -f "$BOOK_PATH" && echo "FILE_OK" || echo "FILE_NOT_FOUND: $BOOK_PATH"
file "$BOOK_PATH" | grep -iE "pdf|epub|zip" \
    && echo "FORMAT_OK" || echo "FORMAT_UNKNOWN"
```

Check the file extension (`.pdf` or `.epub`) or magic bytes (`%PDF` or `PK` zip header).

If the file is not found or the format is not supported, stop with a clear error message listing supported formats.

---

## Step 1.5 — Identify book type

First, check whether Docling is installed (only relevant for PDF files):

```bash
python3 -c "from docling.document_converter import DocumentConverter" 2>/dev/null \
    && echo "DOCLING_AVAILABLE" || echo "DOCLING_MISSING"
```

Then ask the user:

> "What kind of content does this book have? This helps me choose the best extraction method.
>
> 1. **Technical** — has code blocks, tables, formulas, diagrams (e.g. programming books, academic papers, architecture guides)
> 2. **Text-heavy** — mostly prose, few or no tables/code (e.g. management, productivity, narrative non-fiction)
> 3. **Not sure** — I'll use the fast method and warn you if quality seems limited"

Store the answer as `BOOK_TYPE`:
- Option 1 → `BOOK_TYPE=technical`
- Option 2 → `BOOK_TYPE=text`
- Option 3 → `BOOK_TYPE=text`

**If `BOOK_TYPE=technical` and `DOCLING_AVAILABLE`**, inform the user before proceeding:
> "📐 Technical mode selected — using Docling for structure-aware extraction (tables, code blocks, formulas preserved as markdown). This takes ~1.5s per page, so expect a few minutes for longer books. Starting now…"

**If `BOOK_TYPE=technical` and `DOCLING_MISSING`**, inform the user:
> "⚠️  Technical mode selected but Docling is not installed. Falling back to text mode — tables and code blocks will be flattened.
> To enable technical mode: `pip3 install docling`
> Continuing with text mode…"
> Then set `BOOK_TYPE=text`.

**If `BOOK_TYPE=text`**, inform:
> "📄 Text mode selected — using fast extraction (pdftotext). Ready in seconds."

---

## Step 2 — Extract text from PDF or EPUB

Run the extraction script, passing the book type:

```bash
python3 ~/.claude/skills/book-to-skill/scripts/extract.py "$BOOK_PATH" --mode <BOOK_TYPE>
```

- `--mode technical` → uses Docling (layout-aware, preserves tables and code blocks as markdown)
- `--mode text` → uses pdftotext → PyPDF2 → pdfminer fallback chain (fast, plain text)

This creates:
- `/tmp/book_skill_work/full_text.txt` — full extracted text
- `/tmp/book_skill_work/metadata.json` — title, estimated pages, token count, size, extraction_mode

Read `/tmp/book_skill_work/metadata.json` to understand what was extracted.

---

## Step 2.5 — Pre-flight cost estimate

Read `/tmp/book_skill_work/metadata.json` and present the user with an estimate **before doing any generation**:

```
📖 Book detected: <title or filename> by <author or "unknown"> (<format: PDF or EPUB>)
📄 Pages/Spine items: ~<N> | Words: ~<N> | Source tokens: ~<N>K

💰 Estimated token cost (Full Conversion):
   Input  (book reading + prompts): ~<N>K tokens  [estimated_tokens × 1.3]
   Output (skill files generated):  ~<N>K tokens  [chapters × 1,000 + 8,500]
   Total:                           ~<N>K tokens

   Reference prices (2025):
   Claude Sonnet 4.6  $3 / MTok in  · $15 / MTok out  → ~$<X> USD
   Claude Haiku 4.5   $0.80 / MTok in · $4 / MTok out → ~$<X> USD

   ⏱  Estimated time: ~<N> minutes (parallel chapter generation, 4 agents)

📁 Files to be generated:
   SKILL.md + <N> chapter files + glossary + patterns + cheatsheet

➡  Proceed with Full Conversion? (or type "analyze only" to preview first)
```

**How to estimate:**
- Input tokens ≈ `estimated_tokens` from metadata × 1.3 (prompt overhead per chapter pass)
- Output tokens ≈ `chapters_detected` × 1,000 + 4,000 (SKILL.md) + 4,500 (glossary + patterns + cheatsheet)
- Sonnet 4.6: input=$3/MTok output=$15/MTok — Haiku 4.5: input=$0.80/MTok output=$4/MTok
- With 4 parallel agents each sub-agent adds ~2K token system-prompt overhead; add `chapters × 2` to input estimate

Wait for the user to confirm before proceeding. If they say "analyze only", switch to Mode 2.

---

## Step 3 — Analyze book structure

Read `/tmp/book_skill_work/metadata.json` for `title`, `author`, `chapters_detected`,
`has_toc`, and the `chapters` array (already extracted — no need to re-scan the text
for headings).

Then read the first 8,000 characters of `/tmp/book_skill_work/full_text.txt` to confirm:
- **Core themes** and subject domain
- Any chapters or parts missed by the regex (prefaces, appendices, etc.)

If `has_toc` is true in metadata, read that section from the text to get the full chapter list.

**If mode is "Analyze Only":** produce the extraction report now and stop. Structure:
```
## Extraction Report — <Title>

### Author's Core Frameworks
- **<Framework Name>**: <what it is and when to apply>

### Key Principles
- <Principle>: <actionable rule>

### Techniques & Methods
- <Technique>: <step-by-step or how-to>

### Anti-patterns
- <What to avoid>: <why>

### Suggested Skill Name
`{author-lastname}-{core-concept}` — e.g. `cialdini-influence`

### Chapters Detected
| # | Title | Main Frameworks |
```

---

## Step 4 — Ask purpose (Full Conversion only)

Before generating, ask the user:

> "What should this skill help you do? (Pick one or more)
> 1. Apply the author's frameworks while working
> 2. Think with the author's mental models
> 3. Reference specific chapters and concepts
> 4. All of the above"

Use the answer to weight what gets highlighted in the SKILL.md Core section.

---

## Step 5 — Determine skill name

If `$SKILL_NAME_ARG` is non-empty, use it as the skill slug.
Otherwise, propose two options and let the user choose:
- **By author-concept**: `{author-lastname}-{core-concept}` (e.g. `cialdini-influence`, `meadows-systems`)
- **By title**: lowercase hyphens from book title (e.g. `designing-data-intensive-apps`)

Default to author-concept format if the book has a strong methodological identity.

Check that `~/.claude/skills/<skill_name>/` does NOT already exist.
If it does, append `-2` or ask the user before overwriting.

---

## Step 6 — Create skill directory structure

```bash
mkdir -p ~/.claude/skills/<skill_name>/chapters
```

---

## Step 7 — Generate chapter summaries (parallel)

Read `chapters` array from `/tmp/book_skill_work/metadata.json`. Also read
`extraction_mode_used` and `filename` from the same file.

For each chapter entry `c` (with fields `title`, `offset`, `end_offset`, `char_count`),
dispatch a sub-agent via the **Task tool**. Run up to **4 sub-agents in parallel** to
balance throughput against rate limits. Wait for each batch of 4 to complete before
launching the next batch.

Use this prompt template for each sub-agent (substitute values before dispatching):

---
**CHAPTER_TEMPLATE** — pass verbatim as the sub-agent prompt:

```
You are generating ONE chapter summary file for a book skill.

Inputs:
  text_file:    /tmp/book_skill_work/full_text.txt
  offset:       {c.offset}
  end_offset:   {c.end_offset}
  chapter_title: {c.title}
  chapter_index: {NN}   (zero-padded two digits, e.g. 01, 02)
  mode:         {extraction_mode_used}   (technical | text)
  skill_name:   {skill_name}
  output_path:  ~/.claude/skills/{skill_name}/chapters/ch{NN}-{slug}.md
                where {slug} is a lowercase-hyphenated version of chapter_title

Steps:
  1. Read bytes [offset, end_offset] from text_file.
  2. Check: if output_path already exists AND its size > 500 bytes, print
     "Skipping ch{NN} — already complete." and exit successfully.
  3. Write output_path using the CHAPTER FORMAT below.
  4. Confirm: print file size in bytes. If < 500 bytes, print a warning.

CHAPTER FORMAT:
---
# Chapter {NN}: {chapter_title}

## Core Idea
<1–2 sentences: the single most important thing this chapter teaches>

## Frameworks Introduced
- **<Framework Name>**: <exact formulation — preserve the author's naming>
  - When to use: <specific situation>
  - How: <steps or criteria>

## Key Concepts
- **<Term>**: <precise definition in 1 sentence>
(5–10 most important terms from this chapter)

## Mental Models
<2–4 frameworks or thinking tools. Write as "Use X when Y" or "Think of X as Y">

## Anti-patterns
- **<What to avoid>**: <why it fails>

## Code Examples *(omit entirely if mode=text)*
<!-- Copy the most instructive snippet. Preserve indentation exactly. -->
```<language>
<key code example>
```
- **What it demonstrates**: <one line>

## Reference Tables *(omit entirely if mode=text)*
<!-- Reproduce any comparison matrix, parameter table, or decision table in markdown. -->

## Key Takeaways
1. <Actionable insight>
2. <Actionable insight>
3. <Actionable insight>
(3–7 takeaways a practitioner must remember)

## Connects To
- **Ch N**: <why this chapter relates>
- **<Concept>**: <external concept or standard it connects with>
---

Quality rules:
- Extract structure (named frameworks, anti-patterns), not summaries.
- Preserve the author's exact framework names.
- Practitioner voice: "Use X when Y", not "The book explains X".
- Token budget: 800–1,200 tokens per file.
```

---

**Oversized chapters**: If a chapter entry has `"oversized": true` (char_count > 80,000),
split the byte range at the midpoint and dispatch **two** sub-agents instead of one,
writing `ch{NN}a-<slug>.md` and `ch{NN}b-<slug>.md`. Mark both in the Chapter Index.

After all sub-agents return, verify every chapter file:
```bash
for f in ~/.claude/skills/<skill_name>/chapters/ch*.md; do
    size=$(wc -c < "$f")
    [ "$size" -lt 500 ] && echo "INCOMPLETE: $f ($size bytes)"
done
```
Re-dispatch sub-agents for any chapter that is missing or under 500 bytes.

---

## Step 8 — Generate supporting files

### glossary.md
Create `~/.claude/skills/<skill_name>/glossary.md`:
- Every significant term from the book, alphabetically sorted
- Format: `**Term** — definition (Ch N)`
- Max 1,500 tokens

### patterns.md
Create `~/.claude/skills/<skill_name>/patterns.md`:
- All concrete techniques, design patterns, algorithms from the book
- Format: `## Pattern Name\n**When to use**: ...\n**How**: ...\n**Trade-offs**: ...`
- Max 2,000 tokens

### cheatsheet.md
Create `~/.claude/skills/<skill_name>/cheatsheet.md`:
- Decision tables, comparison matrices, quick-reference rules
- The content you'd want on a single printed page
- Max 1,000 tokens

---

## Step 9 — Generate the master SKILL.md

**CRITICAL TOKEN BUDGET: Keep SKILL.md body under 4,000 tokens.**
Compaction truncates from the END — put the most important content FIRST.

Create `~/.claude/skills/<skill_name>/SKILL.md`:

```markdown
---
name: <skill_name>
description: Knowledge base from "<Full Title>" by <Author(s)>. Use when applying <author>'s frameworks for <key topics, 3–6 terms>.
when_to_use: <10–15 trigger phrases based on book topics and terms. Comma-separated.>
allowed-tools: Read Grep
argument-hint: [topic, framework name, or chapter number]
---

# <Full Title>
**Author**: <Author(s)> | **Pages**: ~<N> | **Chapters**: <N> | **Generated**: <YYYY-MM-DD>

## How to Use This Skill

- **Without arguments** — `/skill-name` loads core frameworks for reference
- **With a topic** — `/skill-name replication` → I find and read the relevant chapter
- **With chapter** — `/skill-name ch05` → I load that specific chapter
- **Browse** — ask "what chapters do you have?" to see the full index

When you ask about a topic not covered in Core Frameworks below, I will read
the relevant chapter file before answering.

---

## Core Frameworks & Mental Models
<!-- ~2,000 tokens: the author's most important named frameworks and principles.
     Preserve exact names. Write as "Use X when Y", "Prefer X over Y because Z".
     This is a toolkit, not a summary. -->

<generate 2,000 tokens of the most critical frameworks and insights here>

---

## Chapter Index

| # | Title | Key Frameworks |
|---|-------|----------------|
| [ch01](chapters/ch01-<slug>.md) | <Title> | <framework1>, <framework2> |
| [ch02](chapters/ch02-<slug>.md) | <Title> | <framework1>, <framework2> |
...

## Topic Index

<!-- Alphabetical. Major terms/frameworks → chapter(s) that cover them. -->
- **<Term>** → ch<N>[, ch<N>]
- **<Term>** → ch<N>

## Supporting Files

- [glossary.md](glossary.md) — all key terms with definitions
- [patterns.md](patterns.md) — all techniques and design patterns
- [cheatsheet.md](cheatsheet.md) — quick reference tables and decision guides

---

## Scope & Limits

This skill covers the book content only. For hands-on implementation in your codebase,
combine with project-specific tools. For topics beyond this book, check related skills
or ask Claude directly.
```

---

## Step 10 — Report and cleanup

Report to the user first:

```
✅ Skill created: ~/.claude/skills/<skill_name>/

📚 Book: <Full Title> — <Author>
📄 Pages: ~<N> | Chapters: <N>

Files generated:
  SKILL.md         — core frameworks + index   (~X tokens)
  chapters/        — <N> chapter summaries     (~X tokens each, ~X total)
  glossary.md      — key terms                 (~X tokens)
  patterns.md      — techniques & patterns     (~X tokens)
  cheatsheet.md    — quick reference           (~X tokens)
  ─────────────────────────────────────────────────────
  Total skill size: ~X tokens (loaded on-demand, not all at once)

💡 Tip: run /cost in Claude Code to see the actual token usage for this session.

Usage:
  /<skill_name>                    → load core frameworks
  /<skill_name> <topic>            → find and explain a topic
  /<skill_name> ch<N>              → dive into a specific chapter
```

Then clean up the work directory **only after the user sees the report** and all
chapter files have been verified (Step 7 verification passed):

```bash
rm -rf /tmp/book_skill_work
```

If generation failed partway (rate limit, timeout), do NOT delete `/tmp/book_skill_work`.
The extracted text is needed to resume from Step 7 without re-running extraction.

---

## Quality Rules

1. **Extract structure, not summaries** — capture named frameworks, exact formulations, anti-patterns; not chapter recaps
2. **Preserve the author's precision** — "The 5 Whys" ≠ "ask why multiple times"; keep exact naming
3. **Density over completeness** — a 1,000-token summary beats a 10,000-token excerpt
4. **Practitioner voice** — write "Use X when Y", not "The book explains X"
5. **Front-load SKILL.md** — compaction keeps the first 5,000 tokens; most important content comes first
6. **Chapter files are on-demand** — they don't count against skill budget until loaded
7. **Never copy raw book text** — always synthesize, summarize, extract signal
8. **Topic index is critical** — it's how Claude navigates to the right chapter file
