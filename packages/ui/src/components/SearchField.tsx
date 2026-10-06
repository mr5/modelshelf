import {
  Combobox,
  ComboboxContent,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
  ComboboxTrigger,
} from "@/components/motion/combobox";
import { useState } from "react";

/** Compose beUI's combobox with editable provider IDs and revisions. */
export function SearchField({
  label,
  value,
  options,
  placeholder,
  onChange,
  remote = false,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; description?: string }>;
  placeholder?: string;
  onChange: (value: string) => void;
  remote?: boolean;
}) {
  const [filterTypedValue, setFilterTypedValue] = useState(false);
  const choices =
    value && !options.some((option) => option.value === value)
      ? [{ value, description: "Use this exact value" }, ...options]
      : options;

  return (
    <div className="grid min-w-0 gap-1.5">
      <span className="px-1 text-sm font-medium">{label}</span>
      <Combobox
        value={value}
        query={value}
        onValueChange={onChange}
        onOpenChange={() => setFilterTypedValue(false)}
        filter={remote || !filterTypedValue ? () => true : undefined}
      >
        <ComboboxTrigger>
          <ComboboxInput
            aria-label={label}
            placeholder={placeholder}
            onChange={(event) => {
              setFilterTypedValue(true);
              onChange(event.target.value);
            }}
          />
        </ComboboxTrigger>
        <ComboboxContent>
          <ComboboxList ariaLabel={`${label} suggestions`}>
            {choices.map((option) => (
              <ComboboxItem
                key={option.value}
                value={option.value}
                textValue={option.value}
              >
                <span className="grid min-w-0 gap-0.5">
                  <span className="truncate" title={option.value}>
                    {option.value}
                  </span>
                  {option.description && (
                    <span className="truncate text-xs text-muted-foreground">
                      {option.description}
                    </span>
                  )}
                </span>
              </ComboboxItem>
            ))}
            {choices.length === 0 && (
              <p className="px-3 py-4 text-xs text-muted-foreground">
                Type a value to search, or enter it directly.
              </p>
            )}
          </ComboboxList>
        </ComboboxContent>
      </Combobox>
    </div>
  );
}
