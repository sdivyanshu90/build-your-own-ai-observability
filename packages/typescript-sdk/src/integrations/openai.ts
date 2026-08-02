/**
 * OpenAI-compatible client instrumentation.
 *
 * Wraps the client rather than monkey-patching the module: patching a
 * third-party symbol at import time is how two SDKs end up fighting over it,
 * and how instrumentation breaks silently on a minor version bump.
 *
 * Works with any client exposing the OpenAI shape -- OpenAI, Azure OpenAI,
 * Together, Groq, vLLM, Ollama's compatibility layer, LiteLLM proxies.
 */

import { getClient, type Client, type Span } from "../tracer.js";

interface ChatCompletionsLike {
  create(...args: unknown[]): Promise<unknown> | AsyncIterable<unknown>;
  _aiobsOriginal?: unknown;
}

interface OpenAILike {
  chat: { completions: ChatCompletionsLike };
}

function usageFrom(response: unknown): Record<string, unknown> {
  const usage = (response as { usage?: Record<string, unknown> })?.usage;
  if (!usage) return {};
  const promptDetails = (usage["prompt_tokens_details"] ?? {}) as Record<
    string,
    unknown
  >;
  const completionDetails = (usage["completion_tokens_details"] ??
    {}) as Record<string, unknown>;
  return {
    inputTokens: usage["prompt_tokens"] as number | undefined,
    outputTokens: usage["completion_tokens"] as number | undefined,
    totalTokens: usage["total_tokens"] as number | undefined,
    cachedInputTokens: promptDetails["cached_tokens"] as number | undefined,
    reasoningTokens: completionDetails["reasoning_tokens"] as
      | number
      | undefined,
    // OpenAI counts cached tokens inside prompt_tokens; the platform must know
    // that or it charges them twice.
    raw: { ...usage, cache_convention: "inclusive" },
  };
}

function recordResponse(span: Span, response: unknown): void {
  const usage = usageFrom(response);
  if (Object.keys(usage).length > 0) span.recordUsage(usage as never);
  const id = (response as { id?: string })?.id;
  if (id) span.setAttribute("gen_ai.response.id", id);
  const fingerprint = (response as { system_fingerprint?: string })
    ?.system_fingerprint;
  if (fingerprint)
    span.setAttribute("aiobs.model.system_fingerprint", fingerprint);
  const choices = (
    response as {
      choices?: { message?: { content?: string }; finish_reason?: string }[];
    }
  )?.choices;
  if (choices?.length) {
    const reasons = choices
      .map((choice) => choice.finish_reason)
      .filter(Boolean) as string[];
    if (reasons.length)
      span.setAttribute("gen_ai.response.finish_reasons", reasons);
    const content = choices[0]?.message?.content;
    if (content) span.setOutput(content);
  }
}

/** Return `client` with chat completions traced. Mutates in place and returns it. */
export function instrumentOpenAI<T extends OpenAILike>(
  openaiClient: T,
  options: { client?: Client; provider?: string } = {},
): T {
  const tracer = options.client ?? getClient();
  const provider = options.provider ?? "openai";
  const completions = openaiClient.chat.completions;
  if (completions._aiobsOriginal) return openaiClient;

  const original = completions.create.bind(completions);

  const traced = async (...args: unknown[]): Promise<unknown> => {
    const request = (args[0] ?? {}) as Record<string, unknown>;
    const span = tracer.startSpan(`${provider}.chat`, {
      kind: "client",
      category: "chat_completion",
    });
    span.recordModel({
      provider,
      model: String(request["model"] ?? "unknown"),
      temperature: request["temperature"] as number | undefined,
      topP: request["top_p"] as number | undefined,
      maxTokens: (request["max_tokens"] ?? request["max_completion_tokens"]) as
        | number
        | undefined,
      seed: request["seed"] as number | undefined,
    });
    if (request["messages"]) span.setInput(request["messages"]);

    try {
      const response = await original(...args);
      if (request["stream"]) {
        // The span stays open until the stream is consumed; ending it here
        // would report a duration of ~0 for every streamed call.
        return wrapStream(response as AsyncIterable<unknown>, span);
      }
      recordResponse(span, response);
      span.end();
      return response;
    } catch (error) {
      span.recordException(error);
      span.end();
      throw error;
    }
  };

  (
    traced as ChatCompletionsLike["create"] & { _aiobsOriginal?: unknown }
  )._aiobsOriginal = original;
  completions.create = traced as ChatCompletionsLike["create"];
  completions._aiobsOriginal = original;
  return openaiClient;
}

async function* wrapStream(
  stream: AsyncIterable<unknown>,
  span: Span,
): AsyncIterable<unknown> {
  let chunks = 0;
  const content: string[] = [];
  let usage: Record<string, unknown> = {};
  try {
    for await (const chunk of stream) {
      if (chunks === 0) span.recordFirstToken();
      chunks += 1;
      const extracted = usageFrom(chunk);
      if (Object.values(extracted).some((value) => value !== undefined))
        usage = extracted;
      const delta = (chunk as { choices?: { delta?: { content?: string } }[] })
        ?.choices?.[0]?.delta?.content;
      if (delta) content.push(delta);
      yield chunk;
    }
  } catch (error) {
    span.recordException(error);
    throw error;
  } finally {
    span.setAttribute("aiobs.stream.chunks", chunks);
    if (content.length) span.setOutput(content.join(""));
    if (Object.keys(usage).length > 0) {
      span.recordUsage(usage as never);
    } else {
      // Providers usually omit usage on streams unless asked. Say so rather
      // than implying zero tokens were consumed.
      span.setAttribute("aiobs.usage.source", "missing");
      span.setAttribute(
        "aiobs.stream.usage_missing_hint",
        "pass stream_options: { include_usage: true } to receive token counts",
      );
    }
    span.end();
  }
}

/** Undo `instrumentOpenAI`. Useful between tests. */
export function uninstrumentOpenAI<T extends OpenAILike>(openaiClient: T): T {
  const completions = openaiClient.chat.completions;
  if (completions._aiobsOriginal) {
    completions.create =
      completions._aiobsOriginal as ChatCompletionsLike["create"];
    delete completions._aiobsOriginal;
  }
  return openaiClient;
}
