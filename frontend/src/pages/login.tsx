import { zodResolver } from "@hookform/resolvers/zod"
import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { Bot, LogIn } from "lucide-react"
import { useForm } from "react-hook-form"
import { useNavigate } from "react-router-dom"
import { z } from "zod"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { api } from "@/lib/api"
import { ApiError, type SessionData } from "@/types"

const schema = z.object({
  username: z.string().min(1, "Enter your username."),
  password: z.string().min(1, "Enter your password."),
})
type LoginValues = z.infer<typeof schema>

export function LoginPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const form = useForm<LoginValues>({ resolver: zodResolver(schema), defaultValues: { username: "", password: "" } })
  const [pending, setPending] = React.useState(false)
  const submit = async (values: LoginValues) => {
    setPending(true)
    try {
      const session = await api<SessionData>("/session/login", { method: "POST", json: values })
      queryClient.setQueryData(["session"], session)
      navigate("/", { replace: true })
    } catch (error) {
      if (error instanceof ApiError) {
        form.setError("root", { message: Object.values(error.fields).flat()[0] ?? error.message })
      } else {
        form.setError("root", { message: "The control room could not sign you in." })
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-muted/30 p-3 sm:p-6">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <div className="mx-auto mb-2 flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <Bot className="size-5" />
          </div>
          <CardTitle><h1>Twitch Farm Control Room</h1></CardTitle>
          <CardDescription>Sign in with a staff operator account.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={form.handleSubmit(submit)}>
            <FieldGroup>
              <Field data-invalid={Boolean(form.formState.errors.username)}>
                <FieldLabel htmlFor="username">Username</FieldLabel>
                <Input id="username" autoComplete="username" autoFocus aria-invalid={Boolean(form.formState.errors.username)} {...form.register("username")} />
                <FieldError errors={[form.formState.errors.username]} />
              </Field>
              <Field data-invalid={Boolean(form.formState.errors.password)}>
                <FieldLabel htmlFor="password">Password</FieldLabel>
                <Input id="password" type="password" autoComplete="current-password" aria-invalid={Boolean(form.formState.errors.password)} {...form.register("password")} />
                <FieldError errors={[form.formState.errors.password]} />
              </Field>
              <FieldError errors={[form.formState.errors.root]} />
              <Button type="submit" size="lg" className="min-h-11 w-full" disabled={pending}>
                {pending ? <Spinner /> : <LogIn />} Sign in
              </Button>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
