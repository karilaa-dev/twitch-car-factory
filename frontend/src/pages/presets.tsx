import * as React from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, ListChecks, Plus, Save, Settings2, SlidersHorizontal, Trash2, Users } from "lucide-react"
import { Controller, useForm } from "react-hook-form"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { toast } from "sonner"
import { z } from "zod"

import { ChannelEditor } from "@/components/channel-editor"
import { ChannelList } from "@/components/channel-list"
import { ConfirmAction } from "@/components/confirm-action"
import { InteractiveCard } from "@/components/interactive-card"
import { PageHeader, PageSkeleton } from "@/components/page"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useMediaQuery } from "@/hooks/use-media-query"
import { api, formatTime, mutationError } from "@/lib/api"
import type { PresetDetail, PresetSummary } from "@/types"

interface PresetListData { presets: PresetSummary[] }

export function PresetsPage() {
  const presets = useQuery({ queryKey: ["presets"], queryFn: () => api<PresetListData>("/presets") })
  if (presets.isLoading) return <PageSkeleton />
  return <>
    <PageHeader title="Presets" description="Reusable ordered channel rotations and their account assignments." actions={<Button className="min-h-11 sm:min-h-8" render={<Link to="/presets/new" />}><Plus /> New preset</Button>} />
    {presets.data?.presets.length ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{presets.data.presets.map((preset) => <InteractiveCard key={preset.id} to={`/presets/${preset.id}`} aria-label={`Open ${preset.name}`}><CardHeader><CardTitle>{preset.name}</CardTitle><CardDescription>Updated {formatTime(preset.updated_at)}</CardDescription><CardAction><Badge variant="secondary">{preset.assignment_count} assigned</Badge></CardAction></CardHeader><CardContent><ChannelList channels={preset.channels} limit={6} /></CardContent></InteractiveCard>)}</div> : <Card><CardContent><Empty><EmptyHeader><EmptyMedia variant="icon"><SlidersHorizontal /></EmptyMedia><EmptyTitle>No presets yet</EmptyTitle><EmptyDescription>Create a reusable ordered channel rotation.</EmptyDescription></EmptyHeader><EmptyContent><Button render={<Link to="/presets/new" />}><Plus /> New preset</Button></EmptyContent></Empty></CardContent></Card>}
  </>
}

const presetSchema = z.object({ name: z.string().trim().min(1, "Enter a preset name.").max(150), channels: z.array(z.string()).min(1, "Add at least one channel.") })
type PresetValues = z.infer<typeof presetSchema>

function PresetForm({ initial, submitLabel, pending, onSubmit }: { initial: PresetValues; submitLabel: string; pending: boolean; onSubmit: (values: PresetValues) => void }) {
  const form = useForm<PresetValues>({ resolver: zodResolver(presetSchema), defaultValues: initial })
  return <form onSubmit={form.handleSubmit(onSubmit)}><FieldGroup><Field data-invalid={Boolean(form.formState.errors.name)}><FieldLabel htmlFor="preset-name">Name</FieldLabel><Input id="preset-name" autoComplete="off" placeholder="Night rotation" aria-invalid={Boolean(form.formState.errors.name)} {...form.register("name")} /><FieldError errors={[form.formState.errors.name]} /></Field><Controller name="channels" control={form.control} render={({ field, fieldState }) => <ChannelEditor id="preset-channels" value={field.value} onChange={field.onChange} error={fieldState.error?.message} />} /><FieldError errors={[form.formState.errors.root]} /><Button type="submit" className="min-h-11 self-end" disabled={pending}>{pending ? <Spinner /> : <Save />} {submitLabel}</Button></FieldGroup></form>
}

export function NewPresetPage() {
  const navigate = useNavigate()
  const create = useMutation({
    mutationFn: (values: PresetValues) => api<PresetSummary>("/presets", { method: "POST", json: values }),
    onSuccess: (preset) => { toast.success(`Preset ${preset.name} created.`); navigate(`/presets/${preset.id}`) },
    onError: mutationError,
  })
  return <><PageHeader title="New preset" description="Create a reusable ordered Twitch channel list." actions={<Button variant="outline" className="min-h-11 sm:min-h-8" render={<Link to="/presets" />}><ArrowLeft /> Presets</Button>} /><Card className="max-w-3xl"><CardHeader><CardTitle>Preset configuration</CardTitle><CardDescription>Channel existence is checked as you leave each row and validated again by Django on save.</CardDescription></CardHeader><CardContent><PresetForm initial={{ name: "", channels: [] }} submitLabel="Create preset" pending={create.isPending} onSubmit={(values) => create.mutate(values)} /></CardContent></Card></>
}

export function PresetWorkspacePage() {
  const { id = "" } = useParams()
  const presetId = Number(id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [search, setSearch] = useSearchParams()
  const desktop = useMediaQuery("(min-width: 48rem)")
  const tab = search.get("tab") === "assignments" ? "assignments" : "configuration"
  const preset = useQuery({ queryKey: ["preset", presetId], queryFn: () => api<PresetDetail>(`/presets/${presetId}`), enabled: Number.isInteger(presetId) })
  const update = useMutation({
    mutationFn: (values: PresetValues) => api<PresetDetail>(`/presets/${presetId}`, { method: "PUT", json: values }),
    onSuccess: async () => { toast.success("Preset updated. Desired-running assignments were queued for restart."); await queryClient.invalidateQueries({ queryKey: ["preset", presetId] }); await queryClient.invalidateQueries({ queryKey: ["runtime"] }) },
    onError: mutationError,
  })
  const remove = useMutation({
    mutationFn: () => api(`/presets/${presetId}`, { method: "DELETE", json: {} }),
    onSuccess: () => { toast.success("Preset deleted."); navigate("/presets") },
    onError: mutationError,
  })
  if (preset.isLoading) return <PageSkeleton />
  if (!preset.data) return <Card><CardContent className="py-12 text-center text-sm text-muted-foreground">Preset unavailable.</CardContent></Card>
  const data = preset.data
  const configuration = <Card><CardHeader><CardTitle>Preset configuration</CardTitle><CardDescription>Changing this list increments affected account revisions and queues coalesced restarts where needed.</CardDescription></CardHeader><CardContent><PresetForm key={`${data.id}-${data.updated_at}`} initial={{ name: data.name, channels: data.channels }} submitLabel="Save preset" pending={update.isPending} onSubmit={(values) => update.mutate(values)} /></CardContent></Card>

  return <>
    <PageHeader title={data.name} description={`${data.channels.length} channel${data.channels.length === 1 ? "" : "s"} · ${data.assignment_count} assignment${data.assignment_count === 1 ? "" : "s"}`} actions={<><Button variant="outline" className="min-h-11 sm:min-h-8" render={<Link to="/presets" />}><ArrowLeft /> Presets</Button><ConfirmAction trigger={<Button variant="destructive" className="min-h-11 sm:min-h-8"><Trash2 /> Delete</Button>} title={`Delete ${data.name}?`} description={data.assignment_count ? "This preset is still assigned and Django will refuse deletion until assignments are removed." : "This removes the preset permanently. Existing immutable run history remains unchanged."} confirmLabel="Delete preset" onConfirm={() => remove.mutate()} /></>} />
    {desktop ? <div className="grid items-start gap-4 md:grid-cols-2">{configuration}<Assignments key={data.updated_at} preset={data} /></div> : <Tabs value={tab} onValueChange={(value) => setSearch(value === "configuration" ? {} : { tab: value })}>
        <TabsList variant="line"><TabsTrigger value="configuration"><Settings2 /> Configuration</TabsTrigger><TabsTrigger value="assignments"><Users /> Assignments</TabsTrigger></TabsList>
        <TabsContent value="configuration">{configuration}</TabsContent>
        <TabsContent value="assignments"><Assignments key={data.updated_at} preset={data} /></TabsContent>
      </Tabs>}
  </>
}

function Assignments({ preset }: { preset: PresetDetail }) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = React.useState(() => new Set(preset.assigned_account_ids))
  const mutation = useMutation({
    mutationFn: () => api<PresetDetail>(`/presets/${preset.id}/assignments`, { method: "PUT", json: { account_ids: [...selected] } }),
    onSuccess: async () => { toast.success("Preset assignments updated."); await queryClient.invalidateQueries({ queryKey: ["preset", preset.id] }); await queryClient.invalidateQueries({ queryKey: ["runtime"] }) },
    onError: mutationError,
  })
  const toggle = (id: number, checked: boolean) => setSelected((current) => { const next = new Set(current); if (checked) next.add(id); else next.delete(id); return next })
  return <Card><CardHeader><CardTitle>Account assignments</CardTitle><CardDescription>Assigning or removing a desired-running account queues a validated restart.</CardDescription><CardAction><Badge variant="secondary">{selected.size} selected</Badge></CardAction></CardHeader><CardContent><FieldGroup>{preset.eligible_accounts.length ? <div className="grid gap-2">{preset.eligible_accounts.map((account) => <Field key={account.id} orientation="horizontal"><FieldLabel htmlFor={`account-${account.id}`} className="min-h-11 cursor-pointer"><div><span>{account.username}</span><p className="font-mono text-xs text-muted-foreground">{account.config_key}</p></div></FieldLabel><Checkbox id={`account-${account.id}`} checked={selected.has(account.id)} onCheckedChange={(checked) => toggle(account.id, checked)} /></Field>)}</div> : <Empty><EmptyHeader><EmptyMedia variant="icon"><ListChecks /></EmptyMedia><EmptyTitle>No eligible accounts</EmptyTitle><EmptyDescription>Only active accounts with stored credentials can be assigned.</EmptyDescription></EmptyHeader></Empty>}<Button className="min-h-11 self-end" disabled={mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? <Spinner /> : <Save />} Save assignments</Button></FieldGroup></CardContent></Card>
}
