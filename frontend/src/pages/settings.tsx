import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  ArchiveRestore,
  CheckCircle2,
  Save,
  Settings2,
  Trash2,
  Upload,
} from "lucide-react"
import { useSearchParams } from "react-router-dom"
import { toast } from "sonner"

import { ChannelEditor } from "@/components/channel-editor"
import { PageHeader, PageSkeleton } from "@/components/page"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldTitle,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, formatTime, mutationError } from "@/lib/api"
import type { FarmSettings, ImportPreview } from "@/types"

export function SettingsPage() {
  const [search, setSearch] = useSearchParams()
  const tab = search.get("tab") === "import" ? "import" : "general"
  return (
    <>
      <PageHeader
        title="Settings"
        description="Farm defaults and the staged legacy import workflow."
      />
      <Tabs
        value={tab}
        onValueChange={(value) =>
          setSearch(value === "general" ? {} : { tab: value })
        }
      >
        <TabsList variant="line">
          <TabsTrigger value="general">
            <Settings2 /> General
          </TabsTrigger>
          <TabsTrigger value="import">
            <ArchiveRestore /> Import
          </TabsTrigger>
        </TabsList>
        <TabsContent value="general">
          <GeneralSettings />
        </TabsContent>
        <TabsContent value="import">
          <LegacyImport />
        </TabsContent>
      </Tabs>
    </>
  )
}

function GeneralSettings() {
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<FarmSettings>("/settings/general"),
  })
  if (settings.isLoading) return <PageSkeleton />
  if (!settings.data)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Settings unavailable</AlertTitle>
        <AlertDescription>
          The farm configuration could not be loaded.
        </AlertDescription>
      </Alert>
    )
  return (
    <GeneralSettingsForm
      key={settings.data.updated_at}
      initial={settings.data}
    />
  )
}

function GeneralSettingsForm({ initial }: { initial: FarmSettings }) {
  const queryClient = useQueryClient()
  const [channels, setChannels] = React.useState(initial.default_channels)
  const mutation = useMutation({
    mutationFn: () =>
      api<FarmSettings>("/settings/general", {
        method: "PUT",
        json: {
          default_channels: channels,
          autostart_new_accounts: false,
        },
      }),
    onSuccess: async () => {
      toast.success("Farm settings updated.")
      await queryClient.invalidateQueries({ queryKey: ["settings"] })
      await queryClient.invalidateQueries({ queryKey: ["runtime"] })
    },
    onError: mutationError,
  })
  return (
    <Card className="max-w-3xl">
      <CardHeader>
        <CardTitle>General settings</CardTitle>
        <CardDescription>
          Default-channel changes increment every affected account revision and
          restart desired-running miners.
        </CardDescription>
        <CardAction>
          <span className="text-xs text-muted-foreground">
            Updated {formatTime(initial.updated_at)}
          </span>
        </CardAction>
      </CardHeader>
      <CardContent>
        <FieldGroup>
          <ChannelEditor
            id="default-channels"
            label="Default channels"
            description="Used by every account whose source mode is Default."
            value={channels}
            onChange={setChannels}
          />
          <Button
            className="min-h-11 self-end"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? <Spinner /> : <Save />} Save settings
          </Button>
        </FieldGroup>
      </CardContent>
    </Card>
  )
}

function LegacyImport() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = React.useState<ImportPreview | null>(null)
  const [archive, setArchive] = React.useState<File | null>(null)
  const [acknowledged, setAcknowledged] = React.useState(false)
  const [confirmation, setConfirmation] = React.useState("")
  const [uploadPending, setUploadPending] = React.useState(false)
  const [cancelPending, setCancelPending] = React.useState(false)

  const uploadArchive = async () => {
    if (!archive) return
    setUploadPending(true)
    try {
      const body = new FormData()
      body.append("archive", archive)
      const result = await api<ImportPreview>("/settings/imports", {
        method: "POST",
        body,
      })
      setDraft(result)
      setAcknowledged(false)
      setConfirmation("")
      toast.success("Import preview ready.")
    } catch (error) {
      mutationError(error)
    } finally {
      setUploadPending(false)
    }
  }
  const confirm = useMutation({
    mutationFn: () =>
      api<Record<string, unknown>>(`/settings/imports/${draft?.id}/confirm`, {
        method: "POST",
        json: {
          replace: draft?.preview.requires_replace ?? false,
          acknowledged,
          confirmation,
        },
      }),
    onSuccess: async () => {
      toast.success("Legacy import completed.")
      setDraft(null)
      setArchive(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["settings"] }),
        queryClient.invalidateQueries({ queryKey: ["runtime"] }),
        queryClient.invalidateQueries({ queryKey: ["accounts"] }),
        queryClient.invalidateQueries({ queryKey: ["presets"] }),
      ])
    },
    onError: mutationError,
  })
  const cancelDraft = async () => {
    if (!draft) return
    setCancelPending(true)
    try {
      await api(`/settings/imports/${draft.id}`, {
        method: "DELETE",
        json: {},
      })
      toast.success("Import draft discarded.")
      setDraft(null)
      setArchive(null)
    } catch (error) {
      mutationError(error)
    } finally {
      setCancelPending(false)
    }
  }
  if (!draft)
    return (
      <Card className="max-w-3xl">
        <CardHeader>
          <CardTitle>Legacy backup import</CardTitle>
          <CardDescription>
            Upload one ZIP containing config.yaml, data/state.json, optional
            presets, and cookies. The server inspects it in memory and stores
            only an encrypted, actor-bound draft.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FieldGroup>
            <Alert>
              <ArchiveRestore />
              <AlertTitle>Staged and stopped by default</AlertTitle>
              <AlertDescription>
                No data changes during preview. Imported accounts remain
                stopped, and secrets never appear in the preview.
              </AlertDescription>
            </Alert>
            <Field>
              <FieldLabel htmlFor="legacy-archive">
                Legacy backup ZIP
              </FieldLabel>
              <Input
                id="legacy-archive"
                type="file"
                accept=".zip,application/zip"
                onChange={(event) =>
                  setArchive(event.target.files?.[0] ?? null)
                }
              />
              <FieldDescription>
                Maximum 10 MiB. Archive expansion and document depth are bounded
                server-side.
              </FieldDescription>
            </Field>
            <Button
              className="min-h-11 self-end"
              disabled={!archive || uploadPending}
              onClick={uploadArchive}
            >
              {uploadPending ? <Spinner /> : <Upload />} Preview import
            </Button>
          </FieldGroup>
        </CardContent>
      </Card>
    )

  const preview = draft.preview
  const countRows = Object.entries(preview.counts).filter(
    ([, value]) => value > 0
  )
  const requiresReplace = preview.requires_replace
  const canConfirm =
    preview.can_apply &&
    (!requiresReplace || (acknowledged && confirmation === "REPLACE"))
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Import preview</CardTitle>
          <CardDescription>
            Draft {draft.id} · expires {formatTime(draft.expires_at)}
          </CardDescription>
          <CardAction>
            <Badge variant={preview.can_apply ? "default" : "destructive"}>
              {preview.can_apply ? "Ready" : "Blocked"}
            </Badge>
          </CardAction>
        </CardHeader>
        <CardContent className="grid gap-4">
          {preview.no_op ? (
            <Alert>
              <CheckCircle2 />
              <AlertTitle>No changes required</AlertTitle>
              <AlertDescription>
                The uploaded state already matches the managed database.
              </AlertDescription>
            </Alert>
          ) : null}
          {preview.conflicts.map((item) => (
            <Alert
              key={`${item.subject}:${item.message}`}
              variant="destructive"
            >
              <AlertTriangle />
              <AlertTitle>{item.subject}</AlertTitle>
              <AlertDescription>{item.message}</AlertDescription>
            </Alert>
          ))}
          {preview.warnings.map((item) => (
            <Alert key={item.message}>
              <AlertTriangle />
              <AlertTitle>Import warning</AlertTitle>
              <AlertDescription>{item.message}</AlertDescription>
            </Alert>
          ))}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {countRows.length ? (
              countRows.map(([label, value]) => (
                <Card key={label} size="sm">
                  <CardHeader>
                    <CardDescription>
                      {label.replaceAll("_", " ")}
                    </CardDescription>
                    <CardTitle className="text-2xl tabular-nums">
                      {value}
                    </CardTitle>
                  </CardHeader>
                </Card>
              ))
            ) : (
              <p className="col-span-full text-sm text-muted-foreground">
                No row-level changes.
              </p>
            )}
          </div>
          {preview.destructive_effects.length ? (
            <div className="grid gap-2">
              <h3 className="font-medium">Replacement effects</h3>
              {preview.destructive_effects.map((item) => (
                <div
                  key={`${item.subject}:${item.message}`}
                  className="rounded-lg border p-3 text-sm"
                >
                  <p className="font-medium">{item.subject}</p>
                  <p className="text-muted-foreground">{item.message}</p>
                </div>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>
      {requiresReplace ? (
        <Card>
          <CardHeader>
            <CardTitle>Confirm replacement</CardTitle>
            <CardDescription>
              This reconciles data previously owned by a legacy import. Explicit
              acknowledgement is required.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup>
              <Field orientation="horizontal">
                <FieldLabel htmlFor="ack-replace">
                  <FieldTitle>I understand the replacement effects</FieldTitle>
                  <FieldDescription>
                    Review every destructive effect above before continuing.
                  </FieldDescription>
                </FieldLabel>
                <Checkbox
                  id="ack-replace"
                  checked={acknowledged}
                  onCheckedChange={setAcknowledged}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="replace-confirmation">
                  Type REPLACE
                </FieldLabel>
                <Input
                  id="replace-confirmation"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  autoComplete="off"
                />
                <FieldError>
                  {confirmation && confirmation !== "REPLACE"
                    ? "Confirmation must match REPLACE exactly."
                    : null}
                </FieldError>
              </Field>
            </FieldGroup>
          </CardContent>
        </Card>
      ) : null}
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button
          variant="outline"
          className="min-h-11"
          disabled={cancelPending || confirm.isPending}
          onClick={cancelDraft}
        >
          {cancelPending ? <Spinner /> : <Trash2 />} Cancel draft
        </Button>
        <Button
          className="min-h-11"
          disabled={!canConfirm || confirm.isPending || cancelPending}
          onClick={() => confirm.mutate()}
        >
          {confirm.isPending ? <Spinner /> : <ArchiveRestore />} Confirm import
        </Button>
      </div>
    </div>
  )
}
