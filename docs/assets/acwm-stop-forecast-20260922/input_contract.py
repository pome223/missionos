"""Only current state and a registered option; no realized future action tape."""
import numpy as np
MACRO_ID='smolvla-physical-tower-placement-284-v1'
TAIL=np.array([.08,.008,60,20,60,12,60,72,20])
def build_inputs(*,objects,velocity,physics,robot,count,plan,option,macro_id=MACRO_ID):
 if macro_id!=MACRO_ID or option not in ('continue_hold','bank'):raise ValueError('unregistered option')
 plan=np.asarray(plan,dtype=np.float32)
 if plan.shape!=(12,) or not np.allclose(plan[3:],TAIL):raise ValueError('invalid plan')
 state=np.concatenate([np.asarray(v,dtype=np.float32).ravel() for v in [objects,velocity,physics,robot,count]])
 if state.shape!=(253,) or not np.isfinite(state).all() or not np.isfinite(plan).all():raise ValueError('invalid state')
 tokens=np.zeros((37,7),dtype=np.float32);tokens[:,:3]=plan[:3]
 tokens[:,3]=np.linspace(0,2 if option=='continue_hold' else 1,37)
 tokens[:,4]=2 if option=='continue_hold' else 0
 tokens[:,5]=plan[3];tokens[:,6]=plan[4]
 return state,tokens
