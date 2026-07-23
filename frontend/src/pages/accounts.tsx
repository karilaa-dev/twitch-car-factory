import * as React from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table"
import {
  Archive,
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clipboard,
  ExternalLink,
  History,
  KeyRound,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Square,
  UserRound,
} from "lucide-react"
import { Controller, useForm } from "react-hook-form"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { toast } from "sonner"
import { z } from "zod"

import { ChannelEditor } from "@/components/channel-editor"
import { ChannelList } from "@/components/channel-list"
import { ConfirmAction } from "@/components/confirm-action"
import { CurrentState } from "@/components/current-state"
import {
  InteractiveCard,
  InteractiveTableRow,
} from "@/components/interactive-card"
import { LaunchSource } from "@/components/launch-source"
import { PageHeader, PageSkeleton } from "@/components/page"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldTitle,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, formatTime, mutationError } from "@/lib/api"
import { applyApiFormErrors } from "@/lib/form-errors"
import {
  type AccountDetail,
  type AccountList,
  type AccountSummary,
  type AccountTelemetry,
  type ChannelSource,
} from "@/types"

const accountColumn = createColumnHelper<AccountSummary>()
const sourceModes = [
  { value: "default", label: "Default" },
  { value: "custom", label: "Custom" },
  { value: "preset", label: "Preset" },
] as const

export function AccountsPage() {
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<AccountList>("/accounts"),
    refetchInterval: 5_000,
  })
  const columns = React.useMemo(
    () => [
      accountColumn.accessor("username", {
        header: "Account",
        cell: ({ row }) => (
          <div className="grid gap-0.5">
            <span className="font-medium">{row.original.username}</span>
            <span className="font-mono text-xs text-muted-foreground">
              {row.original.config_key}
            </span>
          </div>
        ),
      }),
      accountColumn.accessor("observed", {
        header: "Current state",
        cell: ({ row }) => (
          <CurrentState
            observed={row.original.observed}
            desired={row.original.desired}
          />
        ),
      }),
      accountColumn.accessor("source.label", {
        header: "Source",
        cell: ({ row }) => (
          <div className="grid max-w-72 gap-1">
            <span>{row.original.source.label}</span>
            <ChannelList channels={row.original.source.channels} limit={3} />
          </div>
        ),
      }),
      accountColumn.accessor("updated_at", {
        header: "Updated",
        cell: (info) => formatTime(info.getValue()),
      }),
    ],
    []
  )
  const table = useReactTable({
    data: accounts.data?.accounts ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  if (accounts.isLoading) return <PageSkeleton />
  if (!accounts.data)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Accounts unavailable</AlertTitle>
        <AlertDescription>
          The account inventory could not be loaded. Try again before making
          changes.
        </AlertDescription>
      </Alert>
    )
  return (
    <>
      <PageHeader
        title="Accounts"
        description={`${accounts.data?.active_count ?? 0} active miner account${accounts.data?.active_count === 1 ? "" : "s"}; inactive legacy records remain visible and read-only.`}
        actions={
          <Button
            className="min-h-11 sm:min-h-8"
            render={<Link to="/accounts/new" />}
          >
            <Plus /> Add account
          </Button>
        }
      />
      <Card>
        <CardContent>
          {accounts.data?.accounts.length ? (
            <>
              <div className="hidden overflow-x-auto md:block">
                <Table>
                  <TableHeader>
                    {table.getHeaderGroups().map((group) => (
                      <TableRow key={group.id}>
                        {group.headers.map((header) => (
                          <TableHead key={header.id}>
                            {flexRender(
                              header.column.columnDef.header,
                              header.getContext()
                            )}
                          </TableHead>
                        ))}
                      </TableRow>
                    ))}
                  </TableHeader>
                  <TableBody>
                    {table.getRowModel().rows.map((row) => (
                      <InteractiveTableRow
                        key={row.id}
                        to={`/accounts/${row.original.id}`}
                        aria-label={`Open ${row.original.username}`}
                      >
                        {row.getVisibleCells().map((cell) => (
                          <TableCell key={cell.id}>
                            {flexRender(
                              cell.column.columnDef.cell,
                              cell.getContext()
                            )}
                          </TableCell>
                        ))}
                      </InteractiveTableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <div className="grid gap-3 md:hidden">
                {accounts.data.accounts.map((account) => (
                  <InteractiveCard
                    key={account.id}
                    size="sm"
                    to={`/accounts/${account.id}`}
                    aria-label={`Open ${account.username}`}
                  >
                    <CardHeader>
                      <CardTitle>{account.username}</CardTitle>
                      <CardDescription className="font-mono">
                        {account.config_key}
                      </CardDescription>
                      <CardAction>
                        <CurrentState
                          observed={account.observed}
                          desired={account.desired}
                        />
                      </CardAction>
                    </CardHeader>
                    <CardContent>
                      <div>
                        <p className="mb-1 text-xs text-muted-foreground">
                          Channel source · {account.source.label}
                        </p>
                        <ChannelList channels={account.source.channels} />
                      </div>
                    </CardContent>
                  </InteractiveCard>
                ))}
              </div>
            </>
          ) : (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <UserRound />
                </EmptyMedia>
                <EmptyTitle>No accounts yet</EmptyTitle>
                <EmptyDescription>
                  Create the first managed Twitch account.
                </EmptyDescription>
              </EmptyHeader>
              <EmptyContent>
                <Button render={<Link to="/accounts/new" />}>
                  <Plus /> Add account
                </Button>
              </EmptyContent>
            </Empty>
          )}
        </CardContent>
      </Card>
    </>
  )
}

const createSchema = z
  .object({
    config_key: z.string().trim().min(1, "Enter an account key.").max(150),
    username: z
      .string()
      .trim()
      .regex(/^[A-Za-z0-9_]{1,100}$/, "Enter a valid Twitch username."),
    password: z.string(),
    mode: z.enum(["default", "custom", "preset"]),
    preset_id: z.number().nullable(),
    channels: z.array(z.string()),
    start_after_save: z.boolean(),
  })
  .superRefine((value, context) => {
    if (value.mode === "preset" && !value.preset_id)
      context.addIssue({
        code: "custom",
        path: ["preset_id"],
        message: "Choose a preset.",
      })
    if (value.mode === "custom" && !value.channels.length)
      context.addIssue({
        code: "custom",
        path: ["channels"],
        message: "Add at least one channel.",
      })
  })
type CreateValues = z.infer<typeof createSchema>

export function NewAccountPage() {
  const navigate = useNavigate()
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<AccountList>("/accounts"),
  })
  const [pending, setPending] = React.useState(false)
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      config_key: "",
      username: "",
      password: "",
      mode: "default",
      preset_id: null,
      channels: [],
      start_after_save: false,
    },
  })
  React.useEffect(() => {
    if (accounts.data)
      form.setValue("start_after_save", accounts.data.autostart_new_accounts)
  }, [accounts.data, form])
  const submit = async (values: CreateValues) => {
    setPending(true)
    try {
      const account = await api<AccountSummary>("/accounts", {
        method: "POST",
        json: values,
      })
      form.reset({ ...values, password: "" })
      toast.success(`Account ${account.config_key} created.`)
      navigate(`/accounts/${account.id}`)
    } catch (error) {
      applyApiFormErrors(error, form.setError, {
        fields: [
          "config_key",
          "username",
          "password",
          "mode",
          "preset_id",
          "channels",
          "start_after_save",
        ],
        aliases: {
          custom_channels: "channels",
          preset: "preset_id",
        },
      })
    } finally {
      setPending(false)
    }
  }
  const mode = form.watch("mode")
  const presetItems = Object.fromEntries(
    (accounts.data?.presets ?? []).map((preset) => [
      String(preset.id),
      preset.name,
    ])
  )

  if (accounts.isLoading) return <PageSkeleton />
  if (!accounts.data)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Account setup unavailable</AlertTitle>
        <AlertDescription>
          Presets and farm defaults could not be loaded, so account creation is
          disabled.
        </AlertDescription>
      </Alert>
    )

  return (
    <>
      <PageHeader
        title="Add account"
        description="Create a passwordless Twitch TV account and choose its initial channel source."
        actions={
          <Button
            variant="outline"
            className="min-h-11 sm:min-h-8"
            render={<Link to="/accounts" />}
          >
            <ArrowLeft /> Accounts
          </Button>
        }
      />
      <Card className="max-w-3xl">
        <CardHeader>
          <CardTitle>Account configuration</CardTitle>
          <CardDescription>
            The internal key is permanent. Twitch authentication happens after
            saving with a short-lived code at twitch.tv/activate.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={form.handleSubmit(submit)}>
            <FieldGroup>
              <div className="grid gap-5 sm:grid-cols-2">
                <Field data-invalid={Boolean(form.formState.errors.config_key)}>
                  <FieldLabel htmlFor="config-key">Account key</FieldLabel>
                  <Input
                    id="config-key"
                    autoComplete="off"
                    placeholder="primary"
                    aria-invalid={Boolean(form.formState.errors.config_key)}
                    {...form.register("config_key")}
                  />
                  <FieldDescription>
                    Permanent identifier for runtime and audit history.
                  </FieldDescription>
                  <FieldError errors={[form.formState.errors.config_key]} />
                </Field>
                <Field data-invalid={Boolean(form.formState.errors.username)}>
                  <FieldLabel htmlFor="username">Twitch username</FieldLabel>
                  <Input
                    id="username"
                    autoComplete="username"
                    placeholder="channel_user"
                    aria-invalid={Boolean(form.formState.errors.username)}
                    {...form.register("username")}
                  />
                  <FieldError errors={[form.formState.errors.username]} />
                </Field>
              </div>
              <Alert>
                <KeyRound />
                <AlertTitle>Passwordless Twitch TV login</AlertTitle>
                <AlertDescription>
                  No Twitch password is stored. After creation, use the Auth tab
                  to connect this account with Twitch&apos;s device activation
                  page.
                </AlertDescription>
              </Alert>
              <Controller
                name="mode"
                control={form.control}
                render={({ field }) => (
                  <Field>
                    <FieldLabel id="initial-channel-source-label">
                      Initial channel source
                    </FieldLabel>
                    <ToggleGroup
                      aria-labelledby="initial-channel-source-label"
                      value={[field.value]}
                      onValueChange={(values) =>
                        values[0] && field.onChange(values[0])
                      }
                      variant="outline"
                      className="flex-wrap"
                    >
                      <ToggleGroupItem
                        value="default"
                        className="min-h-11 sm:min-h-8"
                      >
                        Default
                      </ToggleGroupItem>
                      <ToggleGroupItem
                        value="custom"
                        className="min-h-11 sm:min-h-8"
                      >
                        Custom
                      </ToggleGroupItem>
                      <ToggleGroupItem
                        value="preset"
                        className="min-h-11 sm:min-h-8"
                      >
                        Preset
                      </ToggleGroupItem>
                    </ToggleGroup>
                    <FieldDescription>
                      Default follows farm settings; preset follows the selected
                      reusable rotation.
                    </FieldDescription>
                  </Field>
                )}
              />
              {mode === "preset" ? (
                <Controller
                  name="preset_id"
                  control={form.control}
                  render={({ field, fieldState }) => (
                    <Field data-invalid={Boolean(fieldState.error)}>
                      <FieldLabel
                        id="initial-preset-label"
                        htmlFor="initial-preset"
                      >
                        Preset
                      </FieldLabel>
                      <Select
                        items={presetItems}
                        value={field.value ? String(field.value) : null}
                        onValueChange={(value) =>
                          field.onChange(value ? Number(value) : null)
                        }
                      >
                        <SelectTrigger
                          id="initial-preset"
                          aria-labelledby="initial-preset-label"
                          className="w-full"
                        >
                          <SelectValue placeholder="Select a preset" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {accounts.data?.presets.map((preset) => (
                              <SelectItem
                                key={preset.id}
                                value={String(preset.id)}
                              >
                                {preset.name}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                      <FieldError errors={[fieldState.error]} />
                    </Field>
                  )}
                />
              ) : null}
              {mode === "custom" ? (
                <Controller
                  name="channels"
                  control={form.control}
                  render={({ field, fieldState }) => (
                    <ChannelEditor
                      id="new-account-channels"
                      value={field.value}
                      onChange={field.onChange}
                      error={fieldState.error?.message}
                    />
                  )}
                />
              ) : null}
              {mode === "default" ? (
                <Alert>
                  <Settings2 />
                  <AlertTitle>Farm defaults</AlertTitle>
                  <AlertDescription>
                    <ChannelList
                      channels={accounts.data?.farm_default_channels ?? []}
                      empty="No default channels are configured."
                    />
                  </AlertDescription>
                </Alert>
              ) : null}
              <Controller
                name="start_after_save"
                control={form.control}
                render={({ field }) => (
                  <Field orientation="horizontal">
                    <FieldLabel htmlFor="start-after-save">
                      <FieldTitle>Start after saving</FieldTitle>
                      <FieldDescription>
                        Validate the complete launch source before recording
                        running intent.
                      </FieldDescription>
                    </FieldLabel>
                    <Switch
                      id="start-after-save"
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </Field>
                )}
              />
              <FieldError errors={[form.formState.errors.root]} />
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  render={<Link to="/accounts" />}
                >
                  Cancel
                </Button>
                <Button type="submit" className="min-h-11" disabled={pending}>
                  {pending ? <Spinner /> : <Save />} Create account
                </Button>
              </div>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </>
  )
}

export function AccountWorkspacePage() {
  const { id = "" } = useParams()
  const accountId = Number(id)
  const [search, setSearch] = useSearchParams()
  const tab = ["runtime", "history", "auth"].includes(search.get("tab") ?? "")
    ? search.get("tab")!
    : "runtime"
  const queryClient = useQueryClient()
  const detail = useQuery({
    queryKey: ["account", accountId],
    queryFn: () => api<AccountDetail>(`/accounts/${accountId}`),
    enabled: Number.isInteger(accountId),
  })
  const telemetry = useQuery({
    queryKey: ["account-telemetry", accountId],
    queryFn: () => api<AccountTelemetry>(`/accounts/${accountId}/telemetry`),
    enabled: Number.isInteger(accountId),
    refetchInterval: 2_000,
  })
  const action = useMutation({
    mutationFn: (value: "start" | "stop" | "restart") =>
      api(`/accounts/${accountId}/actions`, {
        method: "POST",
        json: { action: value },
      }),
    onSuccess: async () => {
      toast.success("Lifecycle command queued.")
      await queryClient.invalidateQueries({
        queryKey: ["account-telemetry", accountId],
      })
      await queryClient.invalidateQueries({ queryKey: ["runtime"] })
    },
    onError: mutationError,
  })
  if (detail.isLoading) return <PageSkeleton />
  if (!detail.data)
    return (
      <Alert variant="destructive">
        <Archive />
        <AlertTitle>Account unavailable</AlertTitle>
        <AlertDescription>
          This record does not exist or could not be loaded.
        </AlertDescription>
      </Alert>
    )
  const account = detail.data
  const live = telemetry.data?.account ?? account
  const plannedSource = telemetry.data?.planned_source ?? account.planned_source

  return (
    <>
      <PageHeader
        title={account.username}
        description={`${account.config_key} · ${account.is_active ? "active managed account" : "inactive legacy record"}`}
        actions={
          <>
            <Button
              variant="outline"
              nativeButton={false}
              className="min-h-11 sm:min-h-8"
              render={<Link to="/accounts" />}
            >
              <ArrowLeft /> Accounts
            </Button>
            {account.is_active ? (
              <ButtonGroup>
                <Button
                  variant="outline"
                  className="min-h-11 sm:min-h-8"
                  onClick={() =>
                    action.mutate(live.desired === "running" ? "stop" : "start")
                  }
                >
                  {live.desired === "running" ? <Square /> : <Play />}
                  {live.desired === "running" ? "Stop" : "Start"}
                </Button>
                <Button
                  variant="outline"
                  className="min-h-11 sm:min-h-8"
                  onClick={() => action.mutate("restart")}
                >
                  <RefreshCw /> Restart
                </Button>
              </ButtonGroup>
            ) : null}
          </>
        }
      />
      {!account.is_active ? (
        <Alert>
          <Archive />
          <AlertTitle>Read-only legacy record</AlertTitle>
          <AlertDescription>
            Historical runtime, incident, and launch data remain available;
            configuration and start/restart actions are disabled.
          </AlertDescription>
        </Alert>
      ) : null}
      <Tabs
        value={tab}
        onValueChange={(value) =>
          setSearch(value === "runtime" ? {} : { tab: value })
        }
      >
        <TabsList className="grid w-full grid-cols-3" variant="line">
          <TabsTrigger value="runtime" className="min-w-0 px-1">
            <Play /> Runtime
          </TabsTrigger>
          <TabsTrigger value="history" className="min-w-0 px-1">
            <History /> History
          </TabsTrigger>
          <TabsTrigger value="auth" className="min-w-0 px-1">
            <KeyRound /> Auth<span className="hidden sm:inline"> settings</span>
          </TabsTrigger>
        </TabsList>
        <TabsContent value="runtime">
          <AccountRuntime
            account={account}
            live={live}
            plannedSource={plannedSource}
          />
        </TabsContent>
        <TabsContent value="history">
          <AccountHistory account={account} />
        </TabsContent>
        <TabsContent value="auth">
          <AccountAuthSettings account={account} live={live} />
        </TabsContent>
      </Tabs>
    </>
  )
}

function AccountRuntime({
  account,
  live,
  plannedSource,
}: {
  account: AccountDetail
  live: AccountSummary
  plannedSource: ChannelSource
}) {
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Runtime state</CardTitle>
          <CardDescription>
            Live telemetry and the effective launch source.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <div>
              <dt className="text-xs text-muted-foreground">Current state</dt>
              <dd>
                <CurrentState observed={live.observed} desired={live.desired} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">PID</dt>
              <dd className="font-mono">{live.pid ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Heartbeat</dt>
              <dd>{formatTime(live.last_heartbeat)}</dd>
            </div>
          </dl>
          <Separator />
          <LaunchSource current={live.source} planned={plannedSource} />
        </CardContent>
      </Card>
      <AccountChannelSourceSettings
        key={`${account.id}-${account.updated_at}`}
        account={account}
      />
      {live.open_incident ? (
        <Alert variant="destructive" className="xl:col-span-2">
          <Archive />
          <AlertTitle>{live.open_incident.summary}</AlertTitle>
          <AlertDescription>
            Opened {formatTime(live.open_incident.opened_at)}
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  )
}

const identitySchema = z.object({
  username: z
    .string()
    .regex(/^[A-Za-z0-9_]{1,100}$/, "Enter a valid Twitch username."),
  password: z.string(),
})
type IdentityValues = z.infer<typeof identitySchema>

type AccountSourceDraft = {
  mode: AccountDetail["configuration"]["mode"]
  presetId: number | null
  channels: string[]
}

function updateAccountSourceDraft(
  current: AccountSourceDraft,
  update: Partial<AccountSourceDraft>
) {
  return { ...current, ...update }
}

function AccountChannelSourceSettings({ account }: { account: AccountDetail }) {
  const queryClient = useQueryClient()
  const [source, updateSource] = React.useReducer(updateAccountSourceDraft, {
    mode: account.configuration.mode,
    presetId: account.configuration.preset_id,
    channels: account.configuration.channels,
  })
  const sourceMutation = useMutation({
    mutationFn: () =>
      api<AccountDetail>(`/accounts/${account.id}/channel-source`, {
        method: "PUT",
        json: {
          mode: source.mode,
          preset_id: source.presetId,
          channels: source.channels,
        },
      }),
    onSuccess: async () => {
      toast.success("Channel source updated.")
      await queryClient.invalidateQueries({ queryKey: ["account", account.id] })
      await queryClient.invalidateQueries({
        queryKey: ["account-telemetry", account.id],
      })
    },
    onError: mutationError,
  })
  const presetItems = Object.fromEntries(
    account.presets.map((preset) => [String(preset.id), preset.name])
  )
  const disabled = !account.is_active

  return (
    <Card>
      <CardHeader>
        <CardTitle>Channel source</CardTitle>
        <CardDescription>
          Choose the ordered channels for the next validated launch. Saving
          queues a restart when running is the target.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <FieldGroup>
          <Field data-disabled={disabled}>
            <FieldLabel id="account-source-mode-label">Source mode</FieldLabel>
            <ToggleGroup
              aria-labelledby="account-source-mode-label"
              value={[source.mode]}
              onValueChange={(values) =>
                values[0] &&
                updateSource({
                  mode: values[0] as AccountSourceDraft["mode"],
                })
              }
              variant="outline"
              className="flex-wrap"
            >
              {sourceModes.map((item) => (
                <ToggleGroupItem
                  key={item.value}
                  value={item.value}
                  disabled={disabled}
                  className="min-h-11 sm:min-h-8"
                >
                  {item.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </Field>
          {source.mode === "preset" ? (
            <Field data-disabled={disabled}>
              <FieldLabel
                id="account-source-preset-label"
                htmlFor="account-source-preset"
              >
                Preset
              </FieldLabel>
              <Select
                items={presetItems}
                value={source.presetId ? String(source.presetId) : null}
                onValueChange={(value) =>
                  updateSource({ presetId: value ? Number(value) : null })
                }
                disabled={disabled}
              >
                <SelectTrigger
                  id="account-source-preset"
                  aria-labelledby="account-source-preset-label"
                  className="w-full"
                >
                  <SelectValue placeholder="Select a preset" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {account.presets.map((preset) => (
                      <SelectItem key={preset.id} value={String(preset.id)}>
                        {preset.name}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          ) : null}
          {source.mode === "custom" ? (
            <ChannelEditor
              id="account-channels"
              value={source.channels}
              onChange={(channels) => updateSource({ channels })}
              disabled={disabled}
            />
          ) : null}
          {source.mode === "default" ? (
            <Alert>
              <Settings2 />
              <AlertTitle>Farm defaults</AlertTitle>
              <AlertDescription>
                <ChannelList
                  channels={account.farm_default_channels}
                  empty="No default channels"
                />
              </AlertDescription>
            </Alert>
          ) : null}
          <Button
            className="min-h-11 self-end"
            disabled={disabled || sourceMutation.isPending}
            onClick={() => sourceMutation.mutate()}
          >
            {sourceMutation.isPending ? <Spinner /> : <Save />} Save channel
            source
          </Button>
        </FieldGroup>
      </CardContent>
    </Card>
  )
}

function AccountAuthSettings({
  account,
  live,
}: {
  account: AccountDetail
  live: AccountSummary
}) {
  const queryClient = useQueryClient()
  const authentication = live.authentication
  const identity = useForm<IdentityValues>({
    resolver: zodResolver(identitySchema),
    defaultValues: { username: account.username, password: "" },
  })
  const [identityPending, setIdentityPending] = React.useState(false)
  const submitIdentity = async (values: IdentityValues) => {
    setIdentityPending(true)
    try {
      const data = await api<AccountDetail>(`/accounts/${account.id}`, {
        method: "PATCH",
        json: values,
      })
      identity.reset({ username: data.username, password: "" })
      toast.success("Account identity updated.")
      await queryClient.invalidateQueries({ queryKey: ["account", account.id] })
    } catch (error) {
      identity.setValue("password", "")
      applyApiFormErrors(error, identity.setError, {
        fields: ["username", "password"],
      })
    } finally {
      setIdentityPending(false)
    }
  }
  const disabled = !account.is_active
  const connect = useMutation({
    mutationFn: () =>
      api(`/accounts/${account.id}/authentication/tv`, { method: "POST" }),
    onSuccess: async () => {
      toast.success("Twitch TV authentication started.")
      await queryClient.invalidateQueries({ queryKey: ["account", account.id] })
      await queryClient.invalidateQueries({
        queryKey: ["account-telemetry", account.id],
      })
    },
    onError: mutationError,
  })
  const [remaining, setRemaining] = React.useState(0)
  React.useEffect(() => {
    const update = () =>
      setRemaining(
        authentication.expires_at
          ? Math.max(
              0,
              Math.ceil(
                (new Date(authentication.expires_at).getTime() - Date.now()) /
                  1_000
              )
            )
          : 0
      )
    update()
    if (!authentication.expires_at) return
    const interval = window.setInterval(update, 1_000)
    return () => window.clearInterval(interval)
  }, [authentication.expires_at])
  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60
  const connectButton = (
    <Button
      type="button"
      className="min-h-11"
      disabled={disabled || connect.isPending}
      onClick={live.desired === "running" ? undefined : () => connect.mutate()}
    >
      {connect.isPending ? <Spinner /> : <KeyRound />}
      {authentication.status === "unlinked"
        ? "Connect Twitch"
        : "Reconnect Twitch"}
    </Button>
  )

  if (authentication.method === "twitch_tv") {
    return (
      <Card className="max-w-3xl overflow-hidden">
        <CardHeader className="border-b bg-muted/20">
          <CardTitle>Twitch TV authentication</CardTitle>
          <CardDescription>
            Passwordless device login. Session files stay worker-owned and are
            never returned to the browser.
          </CardDescription>
          <CardAction>
            <span className="rounded-full border px-2.5 py-1 font-mono text-[11px] tracking-wider text-muted-foreground uppercase">
              {authentication.status.replace("_", " ")}
            </span>
          </CardAction>
        </CardHeader>
        <CardContent className="grid gap-5 pt-5">
          <div className="grid gap-1 text-sm sm:grid-cols-[9rem_1fr]">
            <span className="text-muted-foreground">Account</span>
            <span className="font-mono">{account.username}</span>
            <span className="text-muted-foreground">Internal key</span>
            <span className="font-mono">{account.config_key}</span>
          </div>

          {authentication.status === "pending" ? (
            <div className="grid gap-4 rounded-xl border border-primary/35 bg-primary/5 p-4 sm:p-5">
              <div>
                <p className="text-xs font-medium tracking-[0.18em] text-primary uppercase">
                  Activation code
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <code className="text-3xl font-semibold tracking-[0.16em] select-all sm:text-4xl">
                    {authentication.user_code}
                  </code>
                  <Button
                    type="button"
                    size="icon"
                    variant="outline"
                    aria-label="Copy activation code"
                    onClick={async () => {
                      await navigator.clipboard.writeText(
                        authentication.user_code
                      )
                      toast.success("Activation code copied.")
                    }}
                  >
                    <Clipboard />
                  </Button>
                </div>
              </div>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <span className="font-mono text-sm text-muted-foreground">
                  Expires in {minutes}:{String(seconds).padStart(2, "0")}
                </span>
                <a
                  className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium transition-colors hover:bg-muted"
                  href={
                    authentication.activation_url ||
                    "https://www.twitch.tv/activate"
                  }
                  target="_blank"
                  rel="noreferrer"
                >
                  Open twitch.tv/activate <ExternalLink />
                </a>
              </div>
            </div>
          ) : null}

          {authentication.status === "authenticated" ? (
            <Alert>
              <CheckCircle2 />
              <AlertTitle>Twitch connected</AlertTitle>
              <AlertDescription>
                The worker validated the saved Twitch session. Starts and
                restarts will reuse it without requesting another code.
              </AlertDescription>
            </Alert>
          ) : null}

          {authentication.status === "reauth_required" ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Reconnect required</AlertTitle>
              <AlertDescription>
                {authentication.error ||
                  "The saved Twitch session could not be validated. Start a new device login."}
              </AlertDescription>
            </Alert>
          ) : null}

          {authentication.status !== "pending" ? (
            live.desired === "running" ? (
              <ConfirmAction
                trigger={connectButton}
                title="Stop and reconnect this miner?"
                description="The worker will stop the active miner, remove its saved session, and wait for a new Twitch activation code."
                confirmLabel="Stop and reconnect"
                onConfirm={() => connect.mutate()}
              />
            ) : (
              connectButton
            )
          ) : null}
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="max-w-3xl">
      <CardHeader>
        <CardTitle>Authentication settings</CardTitle>
        <CardDescription>
          This legacy account keeps password replacement until it is switched to
          passwordless Twitch TV login. Switching is one-way in this UI.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={identity.handleSubmit(submitIdentity)}>
          <FieldGroup>
            <Field data-disabled>
              <FieldLabel>Account key</FieldLabel>
              <Input
                value={account.config_key}
                disabled
                className="font-mono"
              />
            </Field>
            <Field
              data-invalid={Boolean(identity.formState.errors.username)}
              data-disabled={disabled}
            >
              <FieldLabel htmlFor="edit-username">Twitch username</FieldLabel>
              <Input
                id="edit-username"
                disabled={disabled}
                {...identity.register("username")}
              />
              <FieldError errors={[identity.formState.errors.username]} />
            </Field>
            <Field
              data-invalid={Boolean(identity.formState.errors.password)}
              data-disabled={disabled}
            >
              <FieldLabel htmlFor="edit-password">
                Replace Twitch password
              </FieldLabel>
              <Input
                id="edit-password"
                type="password"
                autoComplete="new-password"
                disabled={disabled}
                {...identity.register("password")}
              />
              <FieldDescription>
                Write-only. Updating identity or credentials restarts a miner
                whose target state is running.
              </FieldDescription>
              <FieldError errors={[identity.formState.errors.password]} />
            </Field>
            <FieldError errors={[identity.formState.errors.root]} />
            <Button
              type="submit"
              className="min-h-11 self-end"
              disabled={disabled || identityPending}
            >
              {identityPending ? <Spinner /> : <KeyRound />} Save authentication
            </Button>
            <Separator />
            <div className="grid gap-2 rounded-lg border p-4">
              <FieldTitle>Switch to Twitch TV login</FieldTitle>
              <FieldDescription>
                This clears the encrypted password and saved session, stops the
                miner if necessary, and requests a device activation code.
              </FieldDescription>
              <ConfirmAction
                trigger={
                  <Button type="button" variant="outline" disabled={disabled}>
                    <KeyRound /> Switch to TV login
                  </Button>
                }
                title="Switch permanently to Twitch TV login?"
                description="The stored password will be erased. The UI will not offer a switch back to password authentication."
                confirmLabel="Switch and connect"
                onConfirm={() => connect.mutate()}
              />
            </div>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  )
}

function AccountHistory({ account }: { account: AccountDetail }) {
  return (
    <div className="grid gap-4">
      <HistoryCard
        title="Incidents"
        empty="No incidents recorded."
        rows={account.incidents.map((item) => ({
          id: item.id,
          primary: item.summary,
          secondary: `${item.status} · ${formatTime(item.opened_at)}`,
          detail: item.details,
        }))}
      />
      <HistoryCard
        title="Immutable runs"
        empty="No launch history recorded."
        rows={account.runs.map((run) => ({
          id: run.id,
          primary: `Run ${run.id} · ${run.source_name}`,
          secondary: `${formatTime(run.started_at)} · revision ${run.channel_revision}`,
          detail: run.error,
          channels: run.channels,
        }))}
      />
      <HistoryCard
        title="Commands"
        empty="No commands recorded."
        rows={account.commands.map((command) => ({
          id: command.id,
          primary: `${command.action} · ${command.status}`,
          secondary: `${command.actor} · ${formatTime(command.created_at)}`,
          detail: command.error || command.reason,
        }))}
      />
    </div>
  )
}

function HistoryCard({
  title,
  empty,
  rows,
}: {
  title: string
  empty: string
  rows: Array<{
    id: number
    primary: string
    secondary: string
    detail: string
    channels?: string[]
  }>
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length ? (
          <div className="grid gap-1">
            {rows.map((row) => (
              <div
                key={row.id}
                className="grid gap-1.5 border-b py-3 last:border-0"
              >
                <p className="font-medium">{row.primary}</p>
                <p className="text-xs text-muted-foreground">{row.secondary}</p>
                {row.channels ? <ChannelList channels={row.channels} /> : null}
                {row.detail ? (
                  <p className="text-sm break-words text-muted-foreground">
                    {row.detail}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <p className="py-8 text-center text-sm text-muted-foreground">
            {empty}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
