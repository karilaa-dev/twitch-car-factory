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
import {
  CheckCircle2,
  CircleHelp,
  GripVertical,
  Plus,
  Trash2,
  XCircle,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldLabel,
} from "@/components/ui/field"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { api } from "@/lib/api"

type ValidationStatus =
  "idle" | "checking" | "exists" | "missing" | "unverified"
type ChannelItem = { id: string; name: string; status: ValidationStatus }

function makeId() {
  return (
    globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)
  )
}

function normalizedChannels(items: ChannelItem[]) {
  const normalized: string[] = []
  const seen = new Set<string>()
  for (const item of items) {
    const name = item.name.trim()
    const folded = name.toLocaleLowerCase()
    if (name && !seen.has(folded)) {
      normalized.push(name)
      seen.add(folded)
    }
  }
  return normalized
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
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
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
    (value.length ? value : [""]).map((name) => ({
      id: makeId(),
      name,
      status: "idle",
    }))
  )
  const itemsRef = React.useRef(items)
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const commit = React.useCallback(
    (next: ChannelItem[], notify = true) => {
      itemsRef.current = next
      setItems(next)
      if (notify) onChange(normalizedChannels(next))
    },
    [onChange]
  )

  const update = (
    itemId: string,
    patch: Partial<ChannelItem>,
    options: { expectedName?: string; notify?: boolean } = {}
  ) => {
    const current = itemsRef.current
    const target = current.find((item) => item.id === itemId)
    if (
      !target ||
      (options.expectedName !== undefined &&
        target.name !== options.expectedName)
    ) {
      return false
    }
    commit(
      current.map((item) =>
        item.id === itemId ? { ...item, ...patch } : item
      ),
      options.notify
    )
    return true
  }

  const validate = async (itemId: string) => {
    const item = itemsRef.current.find((candidate) => candidate.id === itemId)
    if (!item?.name.trim()) return
    const requestedName = item.name
    update(
      item.id,
      { status: "checking" },
      { expectedName: requestedName, notify: false }
    )
    try {
      const result = await api<{ name: string; status: ValidationStatus }>(
        `/channels/validate?name=${encodeURIComponent(requestedName)}`
      )
      update(
        item.id,
        { name: result.name, status: result.status },
        { expectedName: requestedName }
      )
    } catch {
      update(
        item.id,
        { status: "unverified" },
        { expectedName: requestedName, notify: false }
      )
    }
  }

  const dragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return
    const current = itemsRef.current
    const oldIndex = current.findIndex((item) => item.id === active.id)
    const newIndex = current.findIndex((item) => item.id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    commit(arrayMove(current, oldIndex, newIndex))
  }

  return (
    <Field data-invalid={Boolean(error)}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={dragEnd}
      >
        <SortableContext items={items} strategy={verticalListSortingStrategy}>
          <div className="grid gap-2.5">
            {items.map((item, index) => (
              <SortableChannel
                key={item.id}
                item={item}
                inputId={index === 0 ? id : `${id}-${index + 1}`}
                disabled={disabled}
                onChange={(name) => update(item.id, { name, status: "idle" })}
                onRemove={() =>
                  commit(
                    itemsRef.current.filter(
                      (candidate) => candidate.id !== item.id
                    )
                  )
                }
                onValidate={() => void validate(item.id)}
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
        onClick={() =>
          commit(
            [...itemsRef.current, { id: makeId(), name: "", status: "idle" }],
            false
          )
        }
      >
        <Plus /> Add channel
      </Button>
      <FieldDescription>{description}</FieldDescription>
      <FieldError>{error}</FieldError>
    </Field>
  )
}
