import * as React from "react"
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core"
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import { CheckCircle2, CircleHelp, GripVertical, Plus, Trash2, XCircle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { api } from "@/lib/api"

type ValidationStatus = "idle" | "checking" | "exists" | "missing" | "unverified"
type ChannelItem = { id: string; name: string; status: ValidationStatus }

function makeId() {
  return globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)
}

function SortableChannel({
  item,
  inputId,
  disabled,
  onChange,
  onRemove,
  onValidate,
}: {
  item: ChannelItem
  inputId: string
  disabled?: boolean
  onChange: (value: string) => void
  onRemove: () => void
  onValidate: () => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.id,
    disabled,
  })
  const statusIcon =
    item.status === "exists" ? (
      <CheckCircle2 aria-label="Channel exists" />
    ) : item.status === "missing" ? (
      <XCircle aria-label="Channel missing" />
    ) : item.status === "unverified" ? (
      <CircleHelp aria-label="Channel could not be verified" />
    ) : null

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={isDragging ? "relative z-10 opacity-80" : undefined}
    >
      <InputGroup>
        <InputGroupAddon>
          <InputGroupButton
            size="icon-sm"
            aria-label={`Reorder ${item.name || "channel"}`}
            disabled={disabled}
            {...attributes}
            {...listeners}
          >
            <GripVertical />
          </InputGroupButton>
        </InputGroupAddon>
        <InputGroupInput
          className="font-mono"
          id={inputId}
          value={item.name}
          placeholder="twitch_channel"
          spellCheck={false}
          disabled={disabled}
          aria-invalid={item.status === "missing"}
          onChange={(event) => onChange(event.target.value)}
          onBlur={onValidate}
        />
        <InputGroupAddon align="inline-end">
          {statusIcon}
          <InputGroupButton
            size="icon-sm"
            aria-label={`Remove ${item.name || "channel"}`}
            disabled={disabled}
            onClick={onRemove}
          >
            <Trash2 />
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
    </div>
  )
}

export function ChannelEditor({
  id,
  label = "Channels",
  description = "Ordered Twitch channel list. Duplicates are removed when you save.",
  value,
  onChange,
  disabled,
  error,
}: {
  id: string
  label?: string
  description?: string
  value: string[]
  onChange: (channels: string[]) => void
  disabled?: boolean
  error?: string
}) {
  const [items, setItems] = React.useState<ChannelItem[]>(() =>
    (value.length ? value : [""]).map((name) => ({ id: makeId(), name, status: "idle" })),
  )
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const publish = React.useCallback(
    (next: ChannelItem[]) => {
      setItems(next)
      const normalized: string[] = []
      const seen = new Set<string>()
      for (const item of next) {
        const name = item.name.trim()
        const folded = name.toLocaleLowerCase()
        if (name && !seen.has(folded)) {
          normalized.push(name)
          seen.add(folded)
        }
      }
      onChange(normalized)
    },
    [onChange],
  )

  const update = (index: number, patch: Partial<ChannelItem>) => {
    publish(items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)))
  }

  const validate = async (index: number) => {
    const item = items[index]
    if (!item?.name.trim()) return
    update(index, { status: "checking" })
    try {
      const result = await api<{ name: string; status: ValidationStatus }>(
        `/channels/validate?name=${encodeURIComponent(item.name)}`,
      )
      setItems((current) =>
        current.map((candidate) =>
          candidate.id === item.id ? { ...candidate, name: result.name, status: result.status } : candidate,
        ),
      )
    } catch {
      update(index, { status: "unverified" })
    }
  }

  const dragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return
    const oldIndex = items.findIndex((item) => item.id === active.id)
    const newIndex = items.findIndex((item) => item.id === over.id)
    publish(arrayMove(items, oldIndex, newIndex))
  }

  return (
    <Field data-invalid={Boolean(error)}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={dragEnd}>
        <SortableContext items={items} strategy={verticalListSortingStrategy}>
          <div className="grid gap-2.5">
            {items.map((item, index) => (
              <SortableChannel
                key={item.id}
                item={item}
                inputId={index === 0 ? id : `${id}-${index + 1}`}
                disabled={disabled}
                onChange={(name) => update(index, { name, status: "idle" })}
                onRemove={() => publish(items.filter((candidate) => candidate.id !== item.id))}
                onValidate={() => void validate(index)}
              />
            ))}
          </div>
        </SortableContext>
      </DndContext>
      <Button
        type="button"
        variant="outline"
        className="min-h-11 self-start sm:min-h-8"
        disabled={disabled}
        onClick={() => setItems((current) => [...current, { id: makeId(), name: "", status: "idle" }])}
      >
        <Plus /> Add channel
      </Button>
      <FieldDescription>{description}</FieldDescription>
      <FieldError>{error}</FieldError>
    </Field>
  )
}
