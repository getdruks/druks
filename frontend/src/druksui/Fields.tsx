import { useId, useRef, useState } from 'react'

import type { Field, Option } from '../api/types'
import { MarkdownEditor } from './MarkdownEditor'
import { Select } from '../components/Control'

/** Every input in a form, with the value that the operator gave it so far, and what
 * the server said about it. */
export function Fields({
  fields,
  values,
  errors,
  resets,
  onChange,
  onBlur,
}: {
  fields: Field[]
  values: Record<string, unknown>
  errors: Record<string, string>
  resets: number
  onChange: (name: string, value: unknown) => void
  onBlur?: () => void
}) {
  // A page can hold two forms that both take a "body". The id that a label points
  // at thus belongs to this form, not to the field name alone.
  const form = useId()
  return (
    <>
      {fields.map((field) => {
        const id = `${form}${field.name}`
        const help = field.helpText ? `${id}-help` : ''
        const error = errors[field.name] ? `${id}-error` : ''
        const describedBy = [help, error].filter(Boolean).join(' ') || undefined
        // Radio and multi-select are a group of inputs, not one input. The label then
        // names the group. It does not point at an id that no element has.
        const isGroup = field.field === 'radio' || field.field === 'multi_select'
        const name = (
          <>
            {field.label}
            {field.isRequired && <span className="dui-field-required"> *</span>}
          </>
        )
        return (
          <div key={field.name} className="dui-field">
            {isGroup ? (
              <div className="dui-field-label" id={`${id}-label`}>
                {name}
              </div>
            ) : (
              <label className="dui-field-label" htmlFor={id}>
                {name}
              </label>
            )}
            <Input
              field={field}
              id={id}
              value={values[field.name]}
              onChange={onChange}
              onBlur={
                ['text', 'text_area', 'number', 'secret'].includes(field.field) ? onBlur : undefined
              }
              describedBy={describedBy}
              isInvalid={Boolean(errors[field.name])}
              resets={resets}
            />
            {field.helpText && (
              <div className="dui-field-help dim" id={help}>
                {field.helpText}
              </div>
            )}
            {errors[field.name] && (
              <div className="dui-field-error" role="alert" id={error}>
                {errors[field.name]}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}

function Input({
  field,
  id,
  value,
  onChange,
  onBlur,
  describedBy,
  isInvalid,
  resets,
}: {
  field: Field
  id: string
  value: unknown
  onChange: (name: string, value: unknown) => void
  onBlur?: () => void
  describedBy?: string
  isInvalid: boolean
  resets: number
}) {
  const picker = useRef<HTMLInputElement>(null)
  const shared = {
    id,
    // The name has the scope of the id. Two radio groups with one name become one
    // group, and a choice in the second form then clears the first. The form keys the
    // payload that it submits by the field's own name, not by this one.
    name: id,
    autoComplete: 'off',
    required: field.isRequired,
    'aria-describedby': describedBy,
    'aria-invalid': isInvalid || undefined,
  }
  // The label and the description belong to the group as a whole. Invalidity
  // belongs to an input, not to the box around several inputs.
  const group = { 'aria-labelledby': `${id}-label`, 'aria-describedby': describedBy }
  switch (field.field) {
    case 'text':
      return (
        <input
          {...shared}
          className="dui-input"
          type="text"
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
          onBlur={onBlur}
        />
      )
    case 'text_area':
      if (field.isMarkdown) {
        return (
          <MarkdownEditor
            key={resets}
            field={field}
            id={id}
            value={String(value ?? '')}
            onChange={onChange}
            onBlur={onBlur}
            describedBy={describedBy}
            isInvalid={isInvalid}
          />
        )
      }
      return (
        <textarea
          {...shared}
          className="dui-input"
          rows={field.rows}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
          onBlur={onBlur}
        />
      )
    case 'number':
      return (
        <input
          {...shared}
          className="dui-input"
          type="number"
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={field.step ?? undefined}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) =>
            onChange(field.name, event.target.value === '' ? null : Number(event.target.value))
          }
          onBlur={onBlur}
        />
      )
    case 'select': {
      // Options that share a group, one after another, go in one <optgroup>.
      const groups: { group: string; options: Option[] }[] = []
      for (const option of field.options) {
        const group = option.group ?? ''
        const last = groups.at(-1)
        if (last && last.group === group) last.options.push(option)
        else groups.push({ group, options: [option] })
      }
      return (
        <Select
          {...shared}
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
        >
          {field.options.every((option) => option.value !== value) ? (
            <option value="">—</option>
          ) : null}
          {groups.map((entry) =>
            entry.group ? (
              <optgroup key={entry.group} label={entry.group}>
                {entry.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </optgroup>
            ) : (
              entry.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))
            ),
          )}
        </Select>
      )
    }
    case 'multi_select':
      return (
        <MultiSelect
          key={resets}
          field={field}
          id={id}
          chosen={Array.isArray(value) ? (value as string[]) : []}
          onChange={onChange}
          group={group}
        />
      )
    case 'radio':
      return (
        <div className="dui-choices" role="radiogroup" {...group}>
          {field.options.map((option) => (
            <label key={option.value} className="dui-choice">
              <input
                type="radio"
                name={id}
                value={option.value}
                checked={value === option.value}
                onChange={() => onChange(field.name, option.value)}
              />
              {option.label}
            </label>
          ))}
        </div>
      )
    case 'checkbox':
      return (
        <input
          {...shared}
          className="dui-checkbox"
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(field.name, event.target.checked)}
        />
      )
    case 'secret':
      return (
        <input
          {...shared}
          className="dui-input"
          // The same attributes that the settings page gives its bearer token
          // input. The two inputs must stay alike.
          type="password"
          autoComplete="new-password"
          data-1p-ignore=""
          data-lpignore="true"
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
          onBlur={onBlur}
        />
      )
    case 'upload':
      return (
        <input
          {...shared}
          // A reset gives this input a new identity, so the browser drops the file
          // that it holds. A reset of the React state alone does not drop it.
          key={resets}
          className="dui-input dui-upload"
          type="file"
          // onChange sends the picked file itself. The form stores it and submits its
          // id, so this input never carries a value of its own to put back.
          accept={field.accept || undefined}
          onChange={(event) => onChange(field.name, event.target.files?.[0] ?? null)}
        />
      )
    case 'multi_upload': {
      const files = value as File[]
      return (
        <>
          <input
            {...shared}
            key={resets}
            className="dui-input dui-upload"
            type="file"
            multiple
            ref={picker}
            accept={field.accept || undefined}
            required={field.isRequired && files.length === 0}
            aria-required={field.isRequired}
            onChange={(event) => {
              const chosen = Array.from(event.target.files!)
              if (chosen.length) onChange(field.name, chosen)
            }}
          />
          {files.length > 0 && (
            <ul className="dui-upload-files" aria-label={`${field.label} selected files`}>
              {files.map((file, index) => (
                <li key={index}>
                  <span>{file.name}</span>
                  <button
                    type="button"
                    className="druks-button druks-button--compact"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => {
                      const remaining = files.filter((_, one) => one !== index)
                      // The native list of files then matches the list on the page.
                      const selection = new DataTransfer()
                      for (const file of remaining) selection.items.add(file)
                      picker.current!.files = selection.files
                      onChange(field.name, remaining)
                      picker.current!.focus()
                    }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )
    }
    default:
      return (
        <div className="dui-unknown mono" role="alert">
          this dashboard cannot render a {(field as { field: string }).field} field
        </div>
      )
  }
}

/** The checkboxes of a multi-select. A searchable one hides, in the browser, each
 * option whose label does not hold the search. A hidden option keeps its check,
 * and the form still sends it. */
function MultiSelect({
  field,
  id,
  chosen,
  onChange,
  group,
}: {
  field: Extract<Field, { field: 'multi_select' }>
  id: string
  chosen: string[]
  onChange: (name: string, value: unknown) => void
  group: Record<string, string | undefined>
}) {
  const [search, setSearch] = useState('')
  const needle = search.trim().toLowerCase()
  const shown = needle
    ? field.options.filter((option) => option.label.toLowerCase().includes(needle))
    : field.options
  const choices = (
    <div
      className={field.isSearchable ? 'dui-choices dui-choices-list' : 'dui-choices'}
      role="group"
      {...group}
    >
      {shown.map((option) => (
        <label key={option.value} className="dui-choice">
          <input
            type="checkbox"
            name={id}
            value={option.value}
            checked={chosen.includes(option.value)}
            onChange={(event) =>
              onChange(
                field.name,
                event.target.checked
                  ? [...chosen, option.value]
                  : chosen.filter((one) => one !== option.value),
              )
            }
          />
          {option.label}
        </label>
      ))}
      {needle && shown.length === 0 ? <span className="dui-choices-none">No match.</span> : null}
    </div>
  )
  if (!field.isSearchable) return choices
  return (
    <div className="dui-choices-search">
      <input
        className="dui-input"
        type="search"
        aria-label={`Search ${field.label}`}
        placeholder="Search"
        autoComplete="off"
        value={search}
        // Enter in the search narrows the list. It must not submit the form.
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.preventDefault()
        }}
        onChange={(event) => setSearch(event.target.value)}
      />
      {choices}
    </div>
  )
}
