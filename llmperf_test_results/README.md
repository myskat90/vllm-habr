# Ниже — результат тестирования модели Gemma-3

---

## 1. Базовый «эталонный» прогон

> Проверяем среднюю производительность при низкой нагрузке и обычных длинах:

````shell
python3 token_benchmark_ray.py \
    --model "Gemma-3" \
    --mean-input-tokens 550 --stddev-input-tokens 150 \
    --mean-output-tokens 150 --stddev-output-tokens 10 \
    --max-num-completed-requests 30 \
    --num-concurrent-requests 1 \
    --timeout 600 \
    --results-dir "results/baseline" \
    --llm-api openai \
    --additional-sampling-params '{}'
 ````
> **Что измеряем**: latency p50/p95, среднюю inter-token latency, throughput.
>
> **Зачем**: точка сравнения для всех остальных тестов.

### Результаты
| Метрика                         | Значение   |
| ------------------------------- | ---------- |
| `inter_token_latency_s (mean)`  | 0.116 с    |
| `inter_token_latency_s (p95)`   | 0.118 с    |
| `ttft_s (mean)`                 | 3.22 с     |
| `ttft_s (p95)`                  | 3.32 с     |
| `end_to_end_latency_s (mean)`   | 17.11 с    |
| `throughput_token_per_s (mean)` | 9.81 tok/s |

---

## 2. Масштабирование по конкурентности

> Проверяем, как растёт пропускная способность при параллельных запросах:
````shell
for C in 1 4 8 16; do
python3 token_benchmark_ray.py \
    --model "Gemma-3" \
    --mean-input-tokens 550 --stddev-input-tokens 150 \
    --mean-output-tokens 150 --stddev-output-tokens 10 \
    --max-num-completed-requests 60 \
    --num-concurrent-requests $C \
    --timeout 600 \
    --results-dir "results/concurrency_$C" \
    --llm-api openai \
    --additional-sampling-params '{}'
done
````

> **Что измеряем**: общий throughput на 1, 4, 8 и 16 потоков; п95–п99 латентность.
>
> **Зачем**: понять, где начинается деградация и сколько одновременных сессий можно держать.

### Результаты
| Concurrent | Throughput, tok/s | inter\_token\_latency\_s, с | ttft\_s, с |
| ---------- | ----------------- | --------------------------- | ---------- |
| 1          | 9.70              | 0.117                       | 3.16       |
| 4          | 36.87             | 0.113                       | 3.23       |
| 8          | 49.99             | 0.154                       | 8.94       |
| 16         | 45.30             | 0.281                       | 27.78      |

> **Наблюдения:**
> 
> – Пиковый throughput достигается при 8 concurrent (≈50 tok/s),
> 
> – При 16 параллельных сессиях резко растут задержки (ttft ≈28 c).

---

## 3. Зависимость от длины контекста (4 concurrent)

> Смотрим, как ведёт себя модель на коротких, средних и длинных контекстах:
````shell
for L in 512 2048 8192 16384 32768; do
python3 token_benchmark_ray.py \
    --model "Gemma-3" \
    --mean-input-tokens $L --stddev-input-tokens $((L/4)) \
    --mean-output-tokens 150 --stddev-output-tokens 10 \
    --max-num-completed-requests 20 \
    --num-concurrent-requests 4 \
    --timeout 600 \
    --results-dir "results/context_${L}" \
    --llm-api openai \
    --additional-sampling-params '{}'
done
````

> **Что измеряем**: изменения TTFT и end-to-end latency при растущем окне.
>
> **Зачем**: определить практический «потолок» по длине без жёсткого OOM.

### Результаты
| Контекст, tok | Throughput, tok/s | inter\_token\_latency\_s, с | ttft\_s, с |
| ------------- | ----------------- | --------------------------- | ---------- |
| 512           | 32.74             | 0.114                       | 3.22       |
| 2 048         | 30.72             | 0.123                       | 3.82       |
| 8 192         | 21.28             | 0.253                       | 6.84       |
| 16 384        | 10.88             | 0.728                       | 20.24      |
| 32 768        | 4.37              | 0.662                       | 74.82      |

> **Наблюдения:**
> 
> – До 2 048 токенов производительность близка к базовой (≈0.12 с/токен, ≈30 tok/s).
> 
> – При >8 000 токенов существенно падает throughput и растёт latency.
> 
> – Контекст 32 768 токенов приводит к ttft \~75 с и нестабильности (OOM-риск).

---

## 4. Влияние параметров сэмплинга (5 concurrent)

> Сравниваем greedy vs top-k vs top-p:
````shell
declare -A SAMPLERS=(
["greedy"]='{}'
["topk50"]='{"top_k":50}'
["topp0.9"]='{"top_p":0.9}'
["topk50_topp0.9"]='{"top_k":50,"top_p":0.9}'
)
for name in "${!SAMPLERS[@]}"; do
python3 token_benchmark_ray.py \
    --model "Gemma-3" \
    --mean-input-tokens 550 --stddev-input-tokens 150 \
    --mean-output-tokens 150 --stddev-output-tokens 10 \
    --max-num-completed-requests 30 \
    --num-concurrent-requests 5 \
    --timeout 600 \
    --results-dir "results/sampler_${name}" \
    --llm-api openai \
    --additional-sampling-params "${SAMPLERS[$name]}"
done
````

> **Что измеряем**: delta в inter-token latency и throughput при разных стратегиях сэмплинга.
> 
> **Зачем**: подобрать оптимальный compromise скорость ↔ качество.

### Результаты
| Сэмплинг      | Throughput, tok/s | inter\_token\_latency\_s, с | ttft\_s, с |
| ------------- | ----------------- | --------------------------- | ---------- |
| greedy        | 41.41             | 0.115                       | 3.20       |
| Top-K = 50    | 40.30             | 0.117                       | 3.20       |
| Top-P = 0.9   | 41.05             | 0.116                       | 3.19       |
| Top-K + Top-P | 39.89             | 0.119                       | 3.36       |

> **Выводы:**
> – Разница в latency ≤0.004 с/токен, throughput меняется незначительно ≤5 %.
> 
> – Greedy чуть быстрее, но компромисс с качеством может потребовать Top-K/P.

---

## 5. Тест корректности при высокой нагрузке
> Загружаем модель большим количеством коротких запросов, проверяем consistency:
````shell
python3 llm_correctness.py \
    --model "Gemma-3" \
    --max-num-completed-requests 300 \
    --num-concurrent-requests 20 \
    --timeout 600 \
    --results-dir "results/correctness_heavy"
````
> **Что измеряем**: mismatch rate, error rate при 20-кратной конкуренции.
>
> **Зачем**: удостовериться в стабильности выходных данных под нагрузкой.
### Результаты
* **Параметры:** 300 запросов, 20 concurrent
* **Error rate:** 0 %
* **Mismatch rate:** 0 %

> Модель демонстрирует 100 % стабильность и консистентность под нагрузкой.

---

## Итоги

* **Базовая производительность:** выдача токена каждые ≈0.116 с, throughput ≈10 tok/s, время до первого токена ≈3.2 с.
* **Параллелизм:** оптимально использовать до 8 параллельных сессий — дальше латентность растёт непропорционально.
* **Контекст:** безопасный предел — до ≈2–4 к токенов; при больших контекстах существенно падает скорость и возрастает риск ошибки.
* **Сэмплинг:** минимальное влияние на скорость, но для более высокого качества можно выбирать Top-K или Top-P.
* **Надёжность:** при 20 конкурентных запросах отсутствие ошибок и расхождений, что гарантирует стабильность в продакшене.

**Выводы:** Оптимальнее всего на текущей конфигурации запускать Gemma-3 в сценариях с умеренным параллелизмом (≤ 8) и длиной контекста ≤ 4 000 токенов.
