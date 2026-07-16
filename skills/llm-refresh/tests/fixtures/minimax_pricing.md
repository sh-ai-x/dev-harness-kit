<!--
  Fixture: MiniMax pay-as-you-go pricing page, English-translated.

  This file is the parser-test oracle for `parse_minimax_md` in
  skills/llm-refresh/scripts/refresh.py. It mirrors the structure
  of the live vendor page at
    https://platform.minimaxi.com/docs/guides/pricing-paygo.md
  with the original Chinese column labels translated to English. The
  numbers, model IDs, JSX component names (Tabs / Tab / Accordion /
  Info), and markdown structure are unchanged. To refresh after the
  vendor changes pricing, run:

      curl -A Mozilla/5.0 -o skills/llm-refresh/tests/fixtures/minimax_pricing.md \
        https://platform.minimaxi.com/docs/guides/pricing-paygo.md

  then translate any new Chinese column labels the parser relies on.
-->

> ## Documentation Index
> Fetch the complete documentation index at: https://platform.minimaxi.com/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Pay-as-you-go Pricing

> MiniMax pay-as-you-go pricing.

Pay-as-you-go uses a standard open-platform API Key and draws down your
account balance by actual usage. Credits are an independent prepaid
balance used by subscription Keys, with the same resource coverage as
Token Plan. See [Token Plan pricing](/guides/pricing-token-plan) for
credit pricing and usage rules.

## Language Models

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

<Tabs>
  <Tab title="Standard">
    | **Model**                                                                                                                                                                                                  | **Input Price**<br /> CNY/M tokens | **Output Price**<br /> CNY/M tokens | **Cache Read**<br /> CNY/M tokens |
    | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------: | :------------------------: | :------------------------: |
    | **MiniMax-M3**<br />≤ 512k input tokens <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-900/30 dark:text-red-300">50% off forever</span>   |        ~~4.20~~ 2.10       |       ~~16.80~~ 8.40       |        ~~0.84~~ 0.42       |
    | **MiniMax-M3**<br />> 512k input tokens\* <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-900/30 dark:text-red-300">50% off forever</span> |        ~~8.40~~ 4.20       |       ~~33.60~~ 16.80      |        ~~1.68~~ 0.84       |
  </Tab>

  <Tab title="Priority*">
    | **Model**                                                                                                                                                                                                | **Input Price**<br /> CNY/M tokens | **Output Price**<br /> CNY/M tokens | **Cache Read**<br /> CNY/M tokens |
    | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------: | :------------------------: | :------------------------: |
    | **MiniMax-M3**<br />≤ 512k input tokens <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-900/30 dark:text-red-300">50% off forever</span> |        ~~6.30~~ 3.15       |       ~~25.20~~ 12.60      |        ~~1.26~~ 0.63       |
    | **MiniMax-M3**<br />> 512k input tokens <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-900/30 dark:text-red-300">50% off forever</span> |       ~~12.60~~ 6.30       |       ~~50.40~~ 25.20      |        ~~2.52~~ 1.26       |

    \* Priority service gives requests preferential admission, faster
    responses, and lower failure rates. Set `service_tier` to `priority`
    to enable. This tier is billed at 1.5x the standard price.
  </Tab>
</Tabs>

| **Model**                     | **Input Price**<br /> CNY/M tokens | **Output Price**<br /> CNY/M tokens | **Cache Read**<br /> CNY/M tokens | **Cache Write**<br /> CNY/M tokens |
| :------------------------- | :------------------------: | :------------------------: | :------------------------: | :------------------------: |
| **MiniMax-M2.7**           |             2.1            |             8.4            |            0.42            |            2.625           |
| **MiniMax-M2.7-highspeed** |             4.2            |            16.8            |            0.42            |            2.625           |

<Accordion title="Historical Models">
  | **Model**                     | **Input Price**<br /> CNY/M tokens | **Output Price**<br /> CNY/M tokens | **Cache Read**<br /> CNY/M tokens | **Cache Write**<br /> CNY/M tokens |
  | :------------------------- | :------------------------: | :------------------------: | :------------------------: | :------------------------: |
  | **MiniMax-M2.5**           |             2.1            |             8.4            |            0.21            |            2.625           |
  | **MiniMax-M2.5-highspeed** |             4.2            |            16.8            |            0.21            |            2.625           |
  | **MiniMax-M2.1**           |             2.1            |             8.4            |            0.21            |            2.625           |
  | **MiniMax-M2.1-highspeed** |             4.2            |            16.8            |            0.21            |            2.625           |
  | **MiniMax-M2**             |             2.1            |             8.4            |            0.21            |            2.625           |
</Accordion>

<Info>
  Please note:

  1. The billing unit is token count; the token-to-character ratio
     varies slightly by usage scenario. Actual consumption is the source
     of truth; character count includes punctuation.
  2. Token / character ratio (estimate): ~1600 Chinese characters
     consume about 1000 tokens.
</Info>

## Voice

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

MiniMax voice models predict emotional tone and intonation from context
to produce natural, high-fidelity, personalized speech. They serve
social, podcast, audiobook, news, education, and digital-human use cases
with strong expressive capability.

| **Billing Item**                | **Model**         | **Interface Description**                                                                                                       | **Unit Price**<br /> CNY / 10k chars |
| :------------------------- | :------------- | :--------------------------------------------------------------------------------------------------------------------- | :---------------: |
| Sync TTS<br />T2A              | speech-2.8-hd    | Supports volume, pitch, speed, and mixing controls plus bit-rate and sample-rate parameters; returns audio duration and size. Suited to short-text, low-latency scenarios such as chat and dialogue. |        3.5        |
| Sync TTS<br />T2A              | speech-2.8-turbo | Same controls; optimized for low-latency, short-text scenarios such as chat and dialogue.                                       |         2         |
| Async Long-Form TTS<br />T2A Async | speech-2.8-hd | Async text-to-speech generation; single job supports up to 1 million characters; full audio results retrievable asynchronously.        |        3.5        |
| Async Long-Form TTS<br />T2A Async | speech-2.8-turbo | Async text-to-speech generation; single job supports up to 1 million characters.                                                  |         2         |

| **Billing Item**                    | **Model** | **Interface Description**                                                                                                                |                 **Unit Price**<br /> CNY / voice                |
| :--------------------------- | :----- | :----------------------------------------------------------------------------------------------------------------------------- | :--------------------------------------------------------: |
| **Voice Design**<br /> Voice Design  | All models | Generate a voice (voice_id) from a user-supplied description prompt and use that voice in sync / async TTS endpoints.           | 9.9 <br /> No immediate design charge; charged on first synthesis using the generated voice. Test-synthesis: 2 CNY / 10k chars. |
| **Voice Cloning**<br /> Voice Cloning | All models | LLM-driven voice cloning is accurate and fast: no need for hours-long ultra-clean source audio or the long turnaround of classic TTS pipelines. Voice is high-fidelity relative to the original. | 9.9 <br /> No immediate cloning charge; charged on first synthesis using the cloned voice. Test-synthesis billed per chosen model.    |

<Accordion title="Historical Models">
  | **Billing Item**               | **Model**                             | **Unit Price**<br /> CNY / 10k chars |
  | :------------------ | :--------------------------------- | :---------------: |
  | Sync TTS T2A           | speech-2.6-hd / speech-02-hd       |        3.5        |
  | Sync TTS T2A           | speech-2.6-turbo / speech-02-turbo |         2         |
  | Async TTS T2A Async | speech-2.6-hd / speech-02-hd       |        3.5        |
  | Async TTS T2A Async | speech-2.6-turbo / speech-02-turbo |         2         |
</Accordion>

<Info>
  Note: billing unit is character count, in 10,000-character chunks
  (input); one Chinese character counts as 2 characters; English letters,
  Greek letters, punctuation, special characters, spaces, and newlines
  count as 1 character each.
</Info>

## Video

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

| **Model**                  | **Function**                          | **Unit Price**<br /> CNY / video |
| :---------------------- | :----------------- | :---------------- |
| MiniMax-Hailuo-2.3-Fast | image-to-video, 768P 6s       | 1.35              |
| MiniMax-Hailuo-2.3-Fast | image-to-video, 768P 10s      | 2.25              |
| MiniMax-Hailuo-2.3-Fast | image-to-video, 1080P 6s      | 2.31              |
| MiniMax-Hailuo-2.3      | text-to-video, image-to-video, 768P 6s  | 2.00              |
| MiniMax-Hailuo-2.3      | text-to-video, image-to-video, 768P 10s | 4.00              |
| MiniMax-Hailuo-2.3      | text-to-video, image-to-video, 1080P 6s | 3.50              |

<Accordion title="Historical Models">
  | **Model**            | **Function**                          | **Unit Price**<br /> CNY / video |
  | :---------------- | :----------------- | :---------------- |
  | MiniMax-Hailuo-02 | text-to-video, image-to-video, 768P 6s  | 2.00              |
  | MiniMax-Hailuo-02 | text-to-video, image-to-video, 768P 10s | 4.00              |
  | MiniMax-Hailuo-02 | text-to-video, image-to-video, 1080P 6s | 3.50              |
  | MiniMax-Hailuo-02 | image-to-video, 512P 6s       | 0.60              |
  | MiniMax-Hailuo-02 | image-to-video, 512P 10s      | 1.00              |
</Accordion>

## Music

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

| **Model**         | **Interface Description**              | **Unit Price**<br /> CNY / song |
| :------------- | :-------------------- | :--------------: |
| Music-3.0-free | RPM = 3               |        0.0       |
| Music-3.0      | RPM = 120; contact sales to raise |        1.0       |
| Music-2.6-free | RPM = 3               |        0.0       |
| Music-2.6      | RPM = 120; contact sales to raise |        1.0       |
| Lyrics Generation  | Lyrics generation / edit               |       0.05       |

<Accordion title="Historical Models">
  | **Model**     | **Interface Description**              | **Unit Price**<br /> CNY / song |
  | :--------- | :-------------------- | :--------------: |
  | Music-2.5+ | Latest music generation model, instrumental-only unlocked        |        1.0       |
  | Music-2.5  | All-around breakthrough, conduct detail, define realism       |        1.0       |
  | Music-2.0  | Diverse timbres, rich instrument performance           |       0.25       |
</Accordion>

## Image

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

| **Model**                      | **Interface Description**            | **Unit Price**<br /> CNY / image |
| :-------------------------- | :------------------ | :--------------: |
| image-01<br />image-01-live | Generate images from text descriptions or reference images |       0.025      |

## MCP

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

| **Model**  | **Interface Description**                             | **Input Price**<br /> CNY / call |
| :------ | :----------------------------------- | :---------------: |
| API-vlm | Vision interface via **Token Plan MCP** plugin or built-in tools |        0.4        |

When API-vlm is invoked through Token Plan, the call deducts the plan's
included Token Plan quota at the model's pay-as-you-go price; once the
plan quota is exhausted and credits are available, the remainder is
auto-covered by credits.

## Server-side Tools <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-xs font-semibold text-blue-700 before:content-['Beta'] dark:bg-blue-900/30 dark:text-blue-300" />

[Top up now](https://platform.minimaxi.com/user-center/payment/balance)

| **Server-side Tool** | **Interface Description**                                                 | **Unit Price**<br /> CNY / call |
| :-------------- | :------------------------------------------------------- | :--------------: |
| **web\_search** | Web search; the model runs the search server-side and answers based on results. See [server tools](/guides/server-tools). |       0.03       |
