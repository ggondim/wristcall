import { ASK, DENIED, NEEDS_INSTALL, OFF_ON_THIS_DEVICE, ON, UNSUPPORTED, type NotificationsModel } from "../push/notifications"
import { useModel } from "./useModel"

/** Settings: notifications of this device. The caller leaves it out when the site has no Cloud. */
export function NotificationsSetting({ model }: { model: NotificationsModel }) {
  const { screen, granted, busy, error } = useModel(model)
  if (screen === "hidden") return null
  const text =
    screen === "needsInstall"
      ? NEEDS_INSTALL
      : screen === "denied"
        ? DENIED
        : screen === "unsupported"
          ? UNSUPPORTED
          : screen === "on"
            ? ON
            : granted
              ? OFF_ON_THIS_DEVICE
              : ASK
  return (
    <section class="notifications" aria-labelledby="notifications-title">
      <h2 id="notifications-title">Notifications</h2>
      <p class="footnote">{text}</p>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      {screen === "off" ? (
        <div class="actions">
          <button class="button" type="button" disabled={busy} onClick={() => void model.turnOn()}>
            {busy ? "Turning on…" : "Turn on notifications"}
          </button>
        </div>
      ) : null}
    </section>
  )
}
