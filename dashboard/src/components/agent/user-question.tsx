"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";
import type { UserQuestion } from "@/types/api";

interface UserQuestionProps {
  questions: UserQuestion[];
  onAnswer: (questionId: string, answer: string) => Promise<void> | void;
}

export function UserQuestionCard({ questions, onAnswer }: UserQuestionProps) {
  const { t } = useI18n();
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  if (!questions.length) return null;

  const submit = (question: UserQuestion, value: string) => {
    const answer = value.trim();
    if (!answer || question.status === "submitting") return;
    void onAnswer(question.question_id, answer);
  };

  return (
    <div className="flex w-full flex-col gap-2">
      {questions.map((question) => {
        const status = question.status ?? "pending";
        const pending = status === "pending" || status === "submitting";
        const draft = drafts[question.question_id] ?? "";

        return (
          <div
            key={question.question_id}
            className="flex flex-col gap-2 rounded-xl border border-memBorder-primary bg-surface-default-secondary px-3 py-2.5"
          >
            <span className="typo-caption-sm text-onSurface-default-tertiary">
              {t("agent.question.title")}
            </span>
            <p className="whitespace-pre-wrap break-words text-sm text-onSurface-default-primary">
              {question.question}
            </p>

            {status === "answered" ? (
              <p className="whitespace-pre-wrap break-words text-sm text-onSurface-default-secondary">
                <span className="text-onSurface-default-tertiary">
                  {t("agent.question.answered")}:{" "}
                </span>
                {question.answer}
              </p>
            ) : status === "expired" ? (
              <p className="typo-caption-sm text-onSurface-default-tertiary">
                {t("agent.question.expired")}
              </p>
            ) : (
              <>
                {question.options.length ? (
                  <div className="flex flex-wrap gap-2">
                    {question.options.map((option) => (
                      <Button
                        key={option}
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={status === "submitting"}
                        onClick={() => submit(question, option)}
                      >
                        {option}
                      </Button>
                    ))}
                  </div>
                ) : null}

                {question.allow_free_text ? (
                  <div className="flex flex-col gap-2">
                    <Textarea
                      value={draft}
                      textareaClassName="h-20"
                      placeholder={t("agent.question.placeholder")}
                      disabled={status === "submitting"}
                      onChange={(event) =>
                        setDrafts((previous) => ({
                          ...previous,
                          [question.question_id]: event.target.value,
                        }))
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey) {
                          event.preventDefault();
                          submit(question, draft);
                        }
                      }}
                    />
                    <div className="flex justify-end">
                      <Button
                        type="button"
                        size="sm"
                        disabled={status === "submitting" || !draft.trim()}
                        onClick={() => submit(question, draft)}
                      >
                        {status === "submitting"
                          ? t("agent.question.submitting")
                          : t("agent.question.submit")}
                      </Button>
                    </div>
                  </div>
                ) : null}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
