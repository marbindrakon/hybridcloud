import React, { useEffect, useRef, useState } from 'react';
import { TextArea } from '@patternfly/react-core';

export interface ListTextAreaProps {
  id: string;
  value: string[];
  onChange: (next: string[]) => void;
  isDisabled?: boolean;
  placeholder?: string;
  rows?: number;
  validated?: 'default' | 'error';
  'aria-label'?: string;
}

function splitList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Comma- or newline-separated list input. Keeps the raw text while typing (so a trailing separator
 * is not swallowed) and reports the parsed list.
 */
export function ListTextArea({
  id,
  value,
  onChange,
  isDisabled,
  placeholder,
  rows = 2,
  validated,
  'aria-label': ariaLabel,
}: ListTextAreaProps): React.ReactElement {
  const [text, setText] = useState(() => value.join(', '));
  const lastEmitted = useRef<string>(JSON.stringify(value));

  useEffect(() => {
    const incoming = JSON.stringify(value);
    if (incoming !== lastEmitted.current) {
      lastEmitted.current = incoming;
      setText(value.join(', '));
    }
  }, [value]);

  return (
    <TextArea
      id={id}
      value={text}
      isDisabled={isDisabled}
      placeholder={placeholder}
      rows={rows}
      validated={validated}
      aria-label={ariaLabel}
      onChange={(_e, v) => {
        setText(v);
        const next = splitList(v);
        lastEmitted.current = JSON.stringify(next);
        onChange(next);
      }}
    />
  );
}
