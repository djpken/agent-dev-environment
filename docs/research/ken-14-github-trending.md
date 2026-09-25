# KEN-14：i-have-adhd 的 GitHub Trending 排名

- **查閱日：** 2026-09-24
- **候選排名：** GitHub Trending 日榜，不是 GitHub 全站總 stars 排名
- **範圍：** 對照可查到的 Trending 歷史、專案定位與公開社群討論，分析可能的走紅因素。公開資料無法證明單一因果。

## 排名紀錄

Trendshift 的 GitHub Trending 快照指出，`ayghri/i-have-adhd` 於 2026-09-08 首次到日榜第 1。Star History 的歷史列表也記錄 9 月 8 日 All languages 第 1，並記錄 9 月 9 日至 11 日 All languages 第 1、9 月 9 日至 12 日 Python 第 1。兩個追蹤來源相符，支持 issue 所指是 GitHub Trending 的短期榜單位置。[Trendshift 歷史](https://trendshift.io/repositories/30905)、[Star History 排名歷史](https://www.star-history.com/ayghri/i-have-adhd/)

這和累計 stars 的全站排名不同。Star History 記錄 9 月 23 日的 GitHub 全站 Global Rank 為第 486。GitHub 說明 stars 是多種 repository ranking 的依據之一；因此不能把 Trending 第 1 解讀成 GitHub 對程式品質或臨床成效的評比。[Star History](https://www.star-history.com/ayghri/i-have-adhd/)、[GitHub stars 說明](https://docs.github.com/en/get-started/exploring-projects-on-github/saving-repositories-with-stars)

## 可能的走紅因素

1. **解決一個大量使用者熟悉的痛點。** 專案把需求說成「coding agent 把答案藏在冗長回覆裡」，再給 action-first、編號步驟、明示狀態、少前言等可直接採用的規則。這把 ADHD 友善設計連到一般開發者都碰到的 agent verbosity 問題。[官方 README](https://github.com/ayghri/i-have-adhd)
2. **一句話就能理解和分享。** repo 名稱與 README 標語讓效果容易描述，README 也明示不需要 ADHD 診斷才能使用。Hacker News 討論裡有人把簡潔、直接的回覆視為普遍需求，也有人質疑是否只要一句 prompt 就夠。正反兩邊都圍繞一個容易轉述的主張，形成話題性。[Hacker News 討論](https://news.ycombinator.com/item?id=49610631)
3. **採用成本低。** 它把偏好整理成 Markdown skill/plugin，而不是要求安裝獨立服務。README 提供可複製的安裝提示和多種 coding-agent 安裝方式。這降低試用成本；目前 README 可能已在榜單事件後擴充，因此不能把現行每一種整合都當成 9 月 8 日已存在。[官方 README](https://github.com/ayghri/i-have-adhd)、[安裝指南](https://github.com/ayghri/i-have-adhd/blob/main/INSTALL.md)
4. **社群曝光伴隨大幅 star 增長。** GitHub Trending 歷史記錄其 9 月 8 日登頂；Hacker News 的相關貼文於 9 月 9 日出現，頁面查閱時有 542 points 和 371 則留言。該討論晚於第一次登頂，不能當成初次上榜的原因，但能證明它在同期引發了大量跨社群討論。[Trending 歷史](https://trendshift.io/repositories/30905)、[Hacker News 討論](https://news.ycombinator.com/item?id=49610631)

## 判斷限制

較有證據支持的解釋是：清楚、普遍的 agent verbosity 痛點，搭配低成本的試用方式與社群擴散，帶來短期 star velocity，讓 repo 進入 GitHub Trending 頂端。這是根據公開描述和時間線做的推論。GitHub 沒有在目前查到的文件中提供該次 Trending 排名的逐項計分或 referral attribution，因此無法斷言特定貼文、名稱或某一條規則單獨造成第 1 名。

Trending 表示一段時間內受到注意，不足以證明規則改善了代理品質或節省 token。若要評估是否在 ADE 採用，應和現有回覆規則做受控比較；此 issue 只處理「為何上榜」的公開資訊解讀。
