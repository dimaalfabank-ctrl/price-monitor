name: WB Price Monitor

on:
  schedule:
    - cron: '0 */3 * * *'
  workflow_dispatch: {}

jobs:
  check-prices:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install requests playwright
          playwright install --with-deps chromium

      - name: Run price monitor
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python monitor_price.py

      - name: Commit updated price state
        run: |
          git config user.name "wb-price-monitor-bot"
          git config user.email "actions@github.com"
          if [ -f last_prices.json ]; then
            git add last_prices.json
            git diff --quiet --cached || git commit -m "Обновление сохранённых цен [skip ci]"
            git push
          else
            echo "last_prices.json ещё не создан — нечего коммитить."
          fi
