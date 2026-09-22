--[[
	Roblox AI Client ModuleScript
	Coloca este ModuleScript en ServerScriptService o ReplicatedStorage.
	Instrucciones: Reemplaza 'KOYEB_SERVER_URL' con la URL HTTPS de tu app en Koyeb.
--]]

local HttpService = game:GetService("HttpService")

local AIClient = {}

-- CAMBIA ESTA URL POR LA URL DE TU SERVICIO EN KOYEB
local KOYEB_SERVER_URL = "https://tu-app-name.koyeb.app" 

-- Tabla local de conocimiento (Cerebro local en el servidor de Roblox)
local CurrentBrain = {
	version = 0,
	tactics = {
		range_defense_level = 0.5,
		melee_parry_level = 0.3,
		flank_preference = 0.4,
		retreat_threshold = 0.25
	},
	danger_spots = {}
}

-- Telemetría acumulada localmente en la partida
local BatchBuffer = {
	ranged_attacks = 0,
	melee_attacks = 0,
	player_deaths = {},
	boss_victories = 0,
	player_victories = 0
}

-- 1. Descargar los últimos pesos aprendidos desde Koyeb
function AIClient.FetchLatestWeights()
	local success, response = pcall(function()
		return HttpService:GetAsync(KOYEB_SERVER_URL .. "/get-ai-weights")
	end)
	
	if success then
		local data = HttpService:JSONDecode(response)
		if data and data.tactics then
			CurrentBrain = data
			print("[Roblox AI] 🧠 Cerebro actualizado desde Koyeb! Versión:", CurrentBrain.version)
		end
	else
		warn("[Roblox AI] ⚠️ No se pudo conectar a Koyeb. Usando cerebro local por defecto.")
	end
	
	return CurrentBrain
end

-- 2. Registrar eventos durante la partida (Cero lag, solo suma en RAM)
function AIClient.RecordEvent(eventType, eventData)
	if eventType == "ranged_attack" then
		BatchBuffer.ranged_attacks += 1
	elseif eventType == "melee_attack" then
		BatchBuffer.melee_attacks += 1
	elseif eventType == "player_death" and eventData then
		table.insert(BatchBuffer.player_deaths, {x = eventData.X, y = eventData.Y, z = eventData.Z})
	elseif eventType == "boss_win" then
		BatchBuffer.boss_victories += 1
	elseif eventType == "player_win" then
		BatchBuffer.player_victories += 1
	end
end

-- 3. Enviar el lote acumulado a Koyeb para que la IA entrene (Cada 5 min o al terminar partida)
function AIClient.SyncAndTrain()
	local payload = {
		server_id = game.JobId ~= "" and game.JobId or "LocalStudioServer",
		ranged_attacks = BatchBuffer.ranged_attacks,
		melee_attacks = BatchBuffer.melee_attacks,
		player_deaths = BatchBuffer.player_deaths,
		boss_victories = BatchBuffer.boss_victories,
		player_victories = BatchBuffer.player_victories
	}
	
	local success, response = pcall(function()
		return HttpService:PostAsync(
			KOYEB_SERVER_URL .. "/train-ai",
			HttpService:JSONEncode(payload),
			Enum.HttpContentType.ApplicationJson
		)
	end)
	
	if success then
		print("[Roblox AI] 🚀 Telemetría enviada a Koyeb exitosamente!")
		-- Limpiar buffer local
		BatchBuffer = {
			ranged_attacks = 0,
			melee_attacks = 0,
			player_deaths = {},
			boss_victories = 0,
			player_victories = 0
		}
		-- Volver a descargar los pesos recién entrenados
		AIClient.FetchLatestWeights()
	else
		warn("[Roblox AI] ❌ Falló el envío de telemetría a Koyeb.")
	end
end

-- 4. Obtener las tácticas actuales para que los NPCs tomen decisiones
function AIClient.GetTactics()
	return CurrentBrain.tactics
end

-- Autoiniciar: Descargar pesos al iniciar el servidor de Roblox y sincronizar cada 5 minutos
task.spawn(function()
	task.wait(2)
	AIClient.FetchLatestWeights()
	
	while true do
		task.wait(300) -- Cada 5 minutos (300 segundos)
		AIClient.SyncAndTrain()
	end
end)

return AIClient
