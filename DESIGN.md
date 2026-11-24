---
name: RepoPilot 主控台
description: 以參考專案畫出你的新系統：深色操作殼層上的一套施工藍圖
colors:
  shell-bg: "#0f141a"
  panel: "#161d25"
  hairline: "#263240"
  row-hairline: "#222c38"
  ink: "#e3e9f0"
  muted-ink: "#8d9aa8"
  header-bg: "#121820"
  input-bg: "#0f141a"
  input-border: "#3a4757"
  code-bg: "#1f2935"
  soft-bg: "#1a2a3d"
  primary: "#2e6fb8"
  primary-fg: "#ffffff"
  link-blue: "#8cb8ec"
  sheet: "#0b2340"
  sheet-line: "#cfe0f5"
  amber-mark: "#e8a33d"
  amber-soft: "rgba(232, 163, 61, 0.16)"
  ok-bg: "#14301f"
  ok-fg: "#7ddc9a"
  warn-bg: "#3a2c0e"
  warn-fg: "#f2c56b"
  bad-bg: "#3a1a1c"
  bad-fg: "#ff9b9b"
  bad-border: "#6b2b2e"
  light-bg: "#f2f4f7"
  light-panel: "#ffffff"
  light-hairline: "#d3dae3"
  light-ink: "#1b1f24"
  light-muted-ink: "#56616f"
  light-primary: "#12355b"
  light-link-blue: "#1f5fa8"
  light-sheet: "#12355b"
  light-sheet-line: "#e8f0fa"
  light-mark-text: "#8a5300"
  light-focus: "#c27a0e"
typography:
  headline:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "25px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  title:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.3
  subtitle:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
  small:
    fontFamily: "'Noto Sans TC', 'Microsoft JhengHei', system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.55
  mono:
    fontFamily: "Consolas, 'Courier New', monospace"
    fontSize: "12.5px"
    fontWeight: 400
rounded:
  code: "3px"
  control: "4px"
  surface: "6px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-fg}"
    rounded: "{rounded.control}"
    padding: "7px 14px"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.link-blue}"
    rounded: "{rounded.control}"
    padding: "7px 14px"
  button-danger:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.bad-fg}"
    rounded: "{rounded.control}"
    padding: "7px 14px"
  button-small:
    rounded: "{rounded.control}"
    padding: "4px 10px"
    typography: "{typography.label}"
  input:
    backgroundColor: "{colors.input-bg}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "6px 8px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "14px 16px"
  section-tab:
    backgroundColor: "{colors.header-bg}"
    textColor: "{colors.muted-ink}"
    padding: "12px 16px 9px"
  section-tab-selected:
    backgroundColor: "{colors.header-bg}"
    textColor: "{colors.ink}"
  mode-switch-on:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-fg}"
    padding: "5px 14px"
  journey-mark-current:
    backgroundColor: "{colors.amber-mark}"
    textColor: "{colors.light-ink}"
    size: "26px"
  blueprint-sheet:
    backgroundColor: "{colors.sheet}"
    textColor: "{colors.sheet-line}"
    rounded: "{rounded.surface}"
  module-zone-tag:
    textColor: "{colors.amber-mark}"
    rounded: "{rounded.control}"
    padding: "0 6px"
    typography: "{typography.small}"
---

# Design System: RepoPilot 主控台

## Overview

**Creative North Star: "施工藍圖集"**

新專案就是一套施工藍圖。使用者逐題回答，系統把每個設計決定畫進一張有圖框、分區格線與標題欄的配置圖：模組依相依層級長出來，引用參考 repo 的地方畫成詳圖索引圈，未決項目用修訂雲圈起來。外層是安靜的深色操作殼層（預設深色，淺色以同一套角色換值），只有圖紙本身是藍的，只有「此刻要看的地方」是琥珀色。

這是 Operate 模式的工具，不是展示頁：密度中等、資訊直接、以髮絲線分隔而非陰影卡片堆疊。介面分「一般模式」與「開發者模式」，兩者共用同一套視覺；一般模式只是把 API 提示等開發細節收掉（`<html data-mode="general">` 隱藏 `.api-hint`）。

拒絕「左側欄＋聊天泡泡＋右側預覽卡」的業界預設排列：對話只是畫圖的輸入，藍圖才是主舞台。

**Key Characteristics:**
- 深色殼層＋永遠是藍底淺線的藍圖圖紙
- 琥珀色只標「目前」：目前步驟、詳圖索引圈與註記、修訂雲、焦點框
- 系統無襯線中文字、表格數字等寬；圓角 ≤6px；1px 髮絲線、不用陰影卡片
- 頂部四個分頁：新專案／參考專案／問答／設定
- 簽名元件：有分區格線的藍圖圖紙，標題欄隨回答逐列填入

## Colors

冷灰藍的深色殼層、深海軍藍的圖紙、一個稀有的琥珀色標註。

### Primary
- **施工藍（Working Blue）**（`primary`）：主要動作按鈕、已完成步驟的圓、模式切換的選取側、用量條。是「可以按」的顏色，不是「要注意」的顏色。
- **圖紙藍（Drawing Sheet）**（`sheet`；淺色主題 `light-sheet`）：藍圖圖紙底色。兩種主題都是藍底，圖紙不跟著殼層變白。
- **圖線白（Sheet Linework）**（`sheet-line`；淺色主題 `light-sheet-line`）：圖紙上的圖框、模組框、連線、文字。

### Secondary
- **琥珀標註（Amber Mark）**（`amber-mark`，深色主題也作焦點框；淺色主題焦點框為 `light-focus`、文字用 `light-mark-text`）：目前步驟的圓與底線、目前分頁底線、詳圖索引圈、圖上註記框與引線、修訂雲、標題欄剛填入時的閃示、目前問題泡泡的外框、前置條件提示框。
- **琥珀薄霧（Amber Soft）**（`amber-soft`）：選取文字底色、索引圈 hover／展開填色、前置條件提示底。

### Tertiary
- **連結藍（Link Blue）**（`link-blue`；淺色 `light-link-blue`）：連結、次要按鈕文字、可展開摘要文字。

### Neutral
- **殼層底（Shell）**（`shell-bg`）：頁面底色，也用於輸入框底。
- **面板（Panel）**（`panel`）：面板與次要按鈕底。
- **頁首底（Header）**（`header-bg`）：頁首與黏頂分頁列。
- **髮絲線（Hairline）**（`hairline`、`row-hairline`）：面板外框、區塊分隔、表格列線。
- **墨色／淡墨（Ink / Muted Ink）**（`ink`、`muted-ink`）：本文與說明文字。
- **輸入框線（Input Border）**（`input-border`）：輸入框、次要按鈕外框、旅程步驟間的虛線。
- **程式碼底（Code）**（`code-bg`）：行內程式碼、使用者自己的對話泡泡、用量條軌道。
- **狀態色**（`ok-*`、`warn-*`、`bad-*`）：只用於狀態訊息與徽章，不作裝飾。

### Named Rules
**The 琥珀只標此刻 Rule.** 琥珀色只給目前步驟、詳圖索引圈與其註記、修訂雲與焦點框；它出現就代表「看這裡」。不用琥珀作品牌色、按鈕色或一般強調。

**The 圖紙恆藍 Rule.** 藍圖圖紙在兩種主題都是藍底淺線（深色 `sheet`、淺色 `light-sheet`）；圖紙上不放殼層的灰色系。

## Typography

**Body Font:** Noto Sans TC（後援 Microsoft JhengHei、system-ui、sans-serif）
**Label/Mono Font:** Consolas（後援 Courier New、monospace），只用於程式碼、commit、path:line 引用

**Character:** 單一系統無襯線字，以字重與字級分層，像工程圖說的字：清楚、不表演。表格、費用數字與字數計數使用等寬數字（`font-variant-numeric: tabular-nums`）。

### Hierarchy
- **Headline**（700，25px，1.3，-0.01em）：頁面主標（例如新專案工作室標題）。
- **Title**（700，20px，1.3）：區塊標題與品牌名。
- **Subtitle**（700，16px，1.3）：卡片內小標；費用數值也用 16px 粗體。
- **Body**（400，14px，1.55）：本文、表單、旅程步驟。對話問題與輸入區放大到 15px。
- **Label**（13px）：頁首狀態、模式切換、小按鈕、錯誤技術細節。
- **Small**（12.5px；最小 12px）：API 提示、說明、計數、對話說話者標記。藍圖上的格線字母與次要註記可再小到 11～11.5px，只限圖紙內。

### Named Rules
**The 一套字 Rule.** 全介面只有一個無襯線字族加一個等寬字；層級靠 25／20／16／14 與 13／12.5 的字級和字重，不引入展示字體。

## Layout

- 頁首（品牌、一般／開發者模式切換、後端與資料庫狀態、主題）在上；其下是黏頂分頁列（`position: sticky; top: 0`），四個分頁：新專案、參考專案、問答、設定。
- 內容欄最大寬 1440px、置中，內距 20px 24px 48px；區塊之間 16px。
- 新專案工作室：上方為工作室標題與五步旅程列（描述需求、選擇技術、看藍圖、確認計畫、產生專案），下方兩欄：左欄 340～440px（黏頂於 64px）放對話與輸入，右側主舞台放藍圖、選取模組詳情與用量條。
- ≤1100px：工作室改單欄，左欄不再黏頂。
- ≤720px：頁首改兩欄、狀態列換行；分頁列可橫向捲動；內容內距 16px 12px；旅程步驟改為上圖下字、目前步驟底線吃滿格寬；**藍圖改為 2 欄配置**（寬版 4 欄），註記與修訂雲改放圖紙中央下方。
- 間距節奏以 4／8／12／16／24px 為主。

## Elevation & Depth

扁平。深度只靠底色層次（殼層 `shell-bg` → 頁首 `header-bg` → 面板 `panel`）與 1px 髮絲線，面板與卡片不帶陰影。選取狀態以內框線（`box-shadow: inset 0 0 0 1px`，等同描邊）或琥珀外框表示，不以浮起表示。

### Named Rules
**The 髮絲線不浮起 Rule.** 面板、卡片、對話泡泡一律 1px 線框或底色分層，不加外陰影；要強調就換線色（琥珀）或加粗線，不抬高。

## Shapes

- 圓角 ≤6px：行內程式碼 3px，按鈕與輸入框 4px，面板、圖紙、泡泡、提示框 6px。分頁與模式切換內部按鈕為 0。
- 圓只用於有意義的標記：旅程步驟圓（26px）、狀態點（8px）、藍圖詳圖索引圈。
- 虛線有語意：未完成步驟之間的連線、未決問題框（琥珀虛線）、註記引線、空白模組格的幽靈框。
- 修訂雲是唯一的不規則輪廓，只圈未決項目。

## Components

### Buttons
- **Shape:** 小圓角（4px）。
- **Primary:** 施工藍底白字，內距 7px 14px；hover 以 `filter: brightness(1.12)` 微亮；背景與框線 0.15s ease-out 過渡。
- **Secondary:** 面板底、連結藍字、輸入框線色外框。
- **Danger:** 面板底、紅字紅框，只用於移除類動作。
- **Small:** 4px 10px，13px 字。
- **Disabled:** 透明度 0.5、`not-allowed` 游標。
- **Focus:** 3px 琥珀外框（`outline-offset: 2px`；淺色主題用 `light-focus`）。

### Inputs / Fields
- **Style:** 1px 輸入框線、殼層底、4px 圓角、6px 8px 內距；工作室的需求輸入框 15px 字、1.6 行高、10px 12px 內距、可垂直拉伸，下方右對齊等寬字數計數。
- **Focus:** 同全域琥珀焦點框。

### Cards / Containers
- **Corner Style:** 6px。
- **Background:** `panel`。
- **Shadow Strategy:** 無，見 Elevation & Depth。
- **Border:** 1px `hairline`。
- **Internal Padding:** 14px 16px。

### Navigation
- **分頁列：** 頁首底色、底部髮絲線、黏頂。分頁為無底色按鈕，15px 淡墨字，內距 12px 16px 9px；hover 變墨色；選取時墨色粗體並有 3px 琥珀底線。
- **模式切換：** 一組相連按鈕（外框 4px 圓角），選取側施工藍底白字粗體。
- **旅程列：** 26px 圓＋標籤，步驟間以虛線相連；完成為施工藍實心圓，目前為琥珀實心圓（深色字）加 96px、3px 琥珀底線，已關閉步驟為虛線圓。

### Chat（對話）
- 說話者標記 12px 淡墨；使用者泡泡靠右、`code-bg` 底；AI 泡泡靠左、髮絲線框；目前問題的泡泡框線換成琥珀。推薦選項為一列按鈕，下方為自由輸入。

### 藍圖圖紙（Signature）
- 圖紙（`sheet` 底、6px 圓角、可橫向捲動）內為 SVG：2px 外圖框、0.6px 內框，兩框之間是分區格線區，**列用字母（A、B、C…，略過 I、O）、欄用數字**，每個模組的位置可寫成如 `B3` 的分區代號（模組詳情以琥珀小框顯示）。
- 模組依相依層級分列（寬版每列 4 格、窄版 2 格），模組框 1.5px 淺線、極淡填色；hover 填色加深，選取時琥珀 2.5px 框與琥珀薄填色。
- **繪入動效：** 模組框以描邊繪入，`0.9s cubic-bezier(0.16, 1, 0.3, 1)`，依序延遲 70ms；`prefers-reduced-motion: reduce` 時關閉。
- **詳圖索引圈：** 琥珀圓圈內標引用數量；點擊在圖紙上展開註記框（圖紙底、琥珀框、琥珀粗體標題、等寬 path:line 引用），引線以琥珀虛線沿圖框邊緣繞行到註記。
- **修訂雲：** 琥珀描邊的雲形輪廓，圈出未決問題數。
- **標題欄：** 右下表格，隨使用者回答逐列填入技術；剛填入的列以琥珀閃示 1.6s 淡出（減少動態時關閉）；未填列半透明。

### 用量條
- 標籤 12.5px 淡墨、數值 16px 粗體等寬；8px 高、2px 圓角的條，軌道 `code-bg`、填色施工藍。

## Do's and Don'ts

### Do:
- **Do** 把琥珀色（`amber-mark`）只留給目前步驟、詳圖索引圈與註記、修訂雲與焦點框。
- **Do** 讓藍圖圖紙在兩種主題都保持藍底淺線（`sheet`／`light-sheet`）。
- **Do** 以 1px 髮絲線與底色層次分隔區塊，圓角不超過 6px。
- **Do** 表格、費用與計數使用等寬數字。
- **Do** 新動效遵守 `prefers-reduced-motion: reduce`，與藍圖繪入、閃示一致。
- **Do** 開發細節（API 路徑等）放在 `.api-hint`，讓一般模式自動收起。

### Don't:
- **Don't** 用「左側欄＋聊天泡泡＋右側預覽卡」的預設排列取代藍圖主舞台。
- **Don't** 給面板或卡片加外陰影，或使用大於 6px 的圓角。
- **Don't** 把琥珀色當品牌色、主要按鈕色或一般強調色。
- **Don't** 引入第二個無襯線或展示字體。
