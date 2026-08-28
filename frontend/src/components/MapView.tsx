import { Circle, MapContainer, Marker, Popup, TileLayer, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useEffect } from 'react'

/** Crisp CSS dot marker — no external image assets, scales on hidpi. */
const pin = L.divIcon({
  className: 'map-pin',
  html: '<span></span>',
  iconSize: [18, 18],
  iconAnchor: [9, 9],
})

/** Keep the map framed on the resolved location when it changes
 *  (MapContainer's center prop only applies at mount). */
function Recenter({ center }: { center: [number, number] }) {
  const map = useMap()
  useEffect(() => {
    map.setView(center, Math.max(map.getZoom(), 14), { animate: true })
  }, [center, map])
  return null
}

/** Destination map for the structured workflow: one marker at the resolved
 *  location plus a soft neighborhood ring for spatial context. No polygons,
 *  GeoJSON, or AOI internals are ever shown to the user. */
export default function MapView({
  center,
  label,
}: {
  center: [number, number]
  label?: string
}) {
  return (
    <MapContainer
      center={center}
      zoom={14}
      className="map"
      scrollWheelZoom={false}
      attributionControl={true}
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <Circle
        center={center}
        radius={650}
        pathOptions={{
          color: '#e05a2b',
          weight: 1.5,
          opacity: 0.55,
          fillColor: '#e05a2b',
          fillOpacity: 0.07,
        }}
      />
      <Marker position={center} icon={pin}>
        {label ? <Popup>{label}</Popup> : null}
      </Marker>
      <Recenter center={center} />
    </MapContainer>
  )
}
