#!/usr/bin/env bash
# Собрать VK_TOKENS=... из локального файла (по одному токену на строку).
# Файл по умолчанию: backend/vk_tokens_list.txt — в .gitignore, не попадёт в git.
#
# Если бэкап сохранён под другим именем/путём (например ~/Desktop/vk_tokens_backup.txt):
#   bash scripts/vk_tokens_merge_for_env.sh /полный/или/относительный/путь/к/файлу
#
#   1) Положи токены в backend/vk_tokens_list.txt ИЛИ укажи путь к своему файлу первым аргументом
#   2) bash scripts/vk_tokens_merge_for_env.sh
#   3) Скопируй вывод в /root/.tgplay/secrets.env или backend/.env, перезапусти tgplay-backend.
#
# Пустые строки и строки, начинающиеся с #, пропускаются.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILE="${1:-$ROOT/backend/vk_tokens_list.txt}"
if [[ ! -f "$FILE" ]]; then
  echo "Файл не найден: $FILE" >&2
  echo "Создай его и положи по одному VK user token на строку." >&2
  exit 1
fi
tokens=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  [[ "$line" == \#* ]] && continue
  tokens+=("$line")
done < "$FILE"
if [[ ${#tokens[@]} -eq 0 ]]; then
  echo "В $FILE нет ни одной непустой строки (кроме комментариев)." >&2
  exit 1
fi
out=""
for t in "${tokens[@]}"; do
  [[ -n "$out" ]] && out+=","
  out+="$t"
done
echo "VK_TOKENS=$out"
echo "# Вставь строку выше в secrets.env. При нескольких токенах подбери VK_USER_AGENTS_JSON / ||| под тип каждого ключа."
