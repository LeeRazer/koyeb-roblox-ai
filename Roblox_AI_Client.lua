--[[
	GlobalAIDirectorBridge.luau
	Ubicación sugerida: ServerScriptService/ServerLoader/Modules/DungeonSystem/Enemies/GlobalAIDirectorBridge
	
	Integra el AIDirector de tu juego con la IA Global en Koyeb.
--]]

local HttpService = game:GetService("HttpService")

local GlobalAIDirectorBridge = {}

-- CAMBIA ESTA URL POR LA URL FINAL DE TU SERVICIO EN KOYEB
local KOYEB_SERVER_URL = "https://tu-app-koyeb.koyeb.app"

local CurrentGlobalConfig = {
	version = 0,
	director_modifiers = {
		horde_multiplier = 1.0,
		special_cap_bonus = 0,
		stress_accumulation_rate = 1.0,
		interval_speed = 1.0
	},
	zombie_tactics = {}
}

-- 1. Consultar configuración global de la IA desde Koyeb
function GlobalAIDirectorBridge.FetchConfig()
	local success, response = pcall(function()
		return HttpService:GetAsync(KOYEB_SERVER_URL .. "/api/v1/dungeon-ai-config")
	end)
	
	if success then
		local data = HttpService:JSONDecode(response)
		if data and data.director_modifiers then
			CurrentGlobalConfig = data
			print("[GlobalAIDirectorBridge] 🧠 Configuración del Director de IA sincronizada desde Koyeb! v" .. tostring(data.version))
		end
	else
		warn("[GlobalAIDirectorBridge] ⚠️ No se pudo conectar a Koyeb. Usando configuración base del AIDirector.")
	end
	
	return CurrentGlobalConfig
end

-- 2. Reportar el desempeño de la sala/partida al terminar para entrenar a la IA Global
function GlobalAIDirectorBridge.ReportRoomCompletion(telemetryData)
	local payload = {
		server_id = game.JobId ~= "" and game.JobId or "LocalStudioServer",
		difficulty = telemetryData.difficulty or "Normal",
		room_id = telemetryData.room_id or "Room_1",
		players_count = telemetryData.players_count or 1,
		completion_time_seconds = telemetryData.completion_time_seconds or 60.0,
		total_player_deaths = telemetryData.total_player_deaths or 0,
		total_incapacitations = telemetryData.total_incapacitations or 0,
		max_intensity_reached = telemetryData.max_intensity_reached or 0.5,
		top_killer_zombie = telemetryData.top_killer_zombie or "None"
	}
	
	task.spawn(function()
		local success, response = pcall(function()
			return HttpService:PostAsync(
				KOYEB_SERVER_URL .. "/api/v1/report-dungeon-telemetry",
				HttpService:JSONEncode(payload),
				Enum.HttpContentType.ApplicationJson
			)
		end)
		
		if success then
			print("[GlobalAIDirectorBridge] 🚀 Telemetría de sala enviada a Koyeb exitosamente!")
			GlobalAIDirectorBridge.FetchConfig()
		else
			warn("[GlobalAIDirectorBridge] ❌ Error enviando telemetría a Koyeb.")
		end
	end)
end

-- 3. Obtener modificadores para aplicar en AIDirector.luau
function GlobalAIDirectorBridge.GetModifiers()
	return CurrentGlobalConfig.director_modifiers
end

-- 4. Obtener tácticas específicas para los Brains
function GlobalAIDirectorBridge.GetZombieTactics(zombieName)
	if CurrentGlobalConfig.zombie_tactics and CurrentGlobalConfig.zombie_tactics[zombieName] then
		return CurrentGlobalConfig.zombie_tactics[zombieName]
	end
	return nil
end

-- Autoiniciar sincronización
task.spawn(function()
	task.wait(2)
	GlobalAIDirectorBridge.FetchConfig()
end)

return GlobalAIDirectorBridge
