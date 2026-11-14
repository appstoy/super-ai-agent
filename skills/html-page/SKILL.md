---
name: html-page
description: Build a single self-contained HTML page that looks good and opens with no build step.
when_to_use: the user wants a webpage, landing page, or an HTML document
tags: web, html, design
---
# Build a single-file HTML page

Produce ONE `.html` file. No bundler, no npm, no external assets unless the
user asks for them — the page must open by double-clicking it.

## Structure

1. `<!DOCTYPE html>`, `<html lang="zh-CN">`, `<meta charset="utf-8">`, and
   `<meta name="viewport" content="width=device-width, initial-scale=1">`.
2. All CSS inside one `<style>` block. All JS inside one `<script>` block at
   the end of `<body>`.
3. Semantic markup: `<header>`, `<main>`, `<section>`, `<footer>`. Never a pile
   of anonymous `<div>`s.

## Visual rules

- Pick ONE accent colour and use it only for interactive or emphasised
  elements. Everything else stays neutral grey.
- Type scale: body 16px, line-height 1.6, headings 1.25–2× the body size. Set
  a `max-width` around 68ch on text containers so lines stay readable.
- Space things on a 4/8px rhythm. Prefer generous whitespace over borders.
- Support both light and dark with `@media (prefers-color-scheme: dark)` and
  CSS custom properties — never hard-code a colour in a rule.
- Every interactive element needs a visible `:focus-visible` outline. Buttons
  need a `:hover` and an `:active` state.

## Content rules

- Write real copy, not lorem ipsum. If the user gave no text, invent short
  believable copy in their language.
- No placeholder image services (they break offline). Use CSS gradients, inline
  SVG, or nothing.

## Before you finish

- Call `file_write` with the COMPLETE file content and a `.html` path.
- Then verify by reading the first lines back, and reply with the path plus one
  sentence describing what the page contains.
