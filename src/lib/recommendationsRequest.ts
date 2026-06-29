/**
 * Состояние загрузки рекомендаций и отмена запросов (см. App.tsx loadMainRecommendations).
 * Только «текущий» AbortController снимает флаг loading — иначе гонка при abort + новый запрос.
 */
export function shouldClearRecommendationsLoading(
  thisRequest: AbortController,
  currentRequestRef: { current: AbortController | null },
): boolean {
  return currentRequestRef.current === thisRequest;
}
