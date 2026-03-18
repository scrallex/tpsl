import torch

device = torch.device("cuda")

# Simulate one timeframe data
gate_hz = torch.tensor(0.8919, device=device)
gate_rp = torch.tensor(1.0, device=device)
gate_co = torch.tensor(0.2187, device=device)
gate_st = torch.tensor(0.0, device=device)
gate_en = torch.tensor(0.9389, device=device)

# Simulate 3 combos
arr_haz = torch.tensor([0.7, 0.9, 0.95], device=device)
arr_reps = torch.tensor([1, 2, 3], dtype=torch.float32, device=device)
arr_coh = torch.tensor([0.1, 0.1, 0.1], device=device)
arr_stab = torch.tensor([0.0, 0.0, 0.0], device=device)
arr_ent = torch.tensor([2.5, 2.5, 2.5], device=device)

v_hz = gate_hz <= arr_haz
v_rp = gate_rp >= arr_reps
v_co = gate_co >= arr_coh
v_st = gate_st >= arr_stab
v_en = gate_en <= arr_ent

valid_gate = v_hz & v_rp & v_co & v_st & v_en

print(f"Hz: {v_hz}")
print(f"Rp: {v_rp}")
print(f"Co: {v_co}")
print(f"St: {v_st}")
print(f"En: {v_en}")
print(f"Valid: {valid_gate}")
