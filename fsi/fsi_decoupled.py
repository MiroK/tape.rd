from cbcflow import NSScheme
from cbcflow.schemes.utils import NSSpacePoolSplit, make_velocity_bcs, make_pressure_bcs
from common import BoundaryProlongator, BoundaryRestrictor
from dolfin import *
from utils import *


class FSI_Decoupled(NSScheme):
    @classmethod
    def default_params(cls):
        params = NSScheme.default_params()
        params.update(
                # Default to P2-P1
                u_degree = 2,
                p_degree = 1,
                r = 2, # Extrapolation degree of velocity
                s = 1, # Extrapolation degree of pressure and displacement
                tol = 1E-8       # Abs/rel tolerance for Krylov solvers
                )
        return params

    def solve(self, problem, timer):
        assert isinstance(problem, FSIProblem)

        mesh = problem.mesh
        bmesh = problem.bmesh
        # emap is used to get the Prolongators/Restrictors
        emap = problem.emap

        dim = mesh.geometry().dim()

        dx = problem.dx
        ds = problem.ds

        n = FacetNormal(mesh)

        dt, timesteps, start_timestep = compute_regular_timesteps(problem)
        dt = Constant(dt)
        t = Constant(timesteps[start_timestep], name="TIME")

        # Function spaces
        spaces = NSSpacePoolSplit(mesh, self.params.u_degree, self.params.p_degree)
        V = spaces.V
        Q = spaces.Q
        D = spaces.spacepool.get_custom_space("CG", 1, (dim,))  # For mesh displ.
        Dgb = VectorFunctionSpace(bmesh, 'CG', 1)                # For solid displ.

        tolerance = self.params.tol
        # Operators for transfering data
        # Computing normal as a function in Dgb/computing traction
        D_to_Dgb = BoundaryRestrictor(D, emap, Dgb, tol=tolerance)
        # Prolongating displacement to D
        Dgb_to_D = BoundaryProlongator(Dgb, emap, D, tol=tolerance)
        # Get the normal
        nDgb = Function(Dgb)
        D_to_Dgb.map(n, nDgb)

        # Trial- and testfunctions
        u, v = TrialFunction(V), TestFunction(V)              # Velocity
        p, q = TrialFunction(Q), TestFunction(Q)              # Pressure
        d, e = TrialFunction(D), TestFunction(D)              # Mesh displacement

        # Solution functions
        U = Function(V)               # Velocity
        P = Function(Q)               # Pressure
        DF = Function(D)              # Fluid (full mesh) displacement
        Usolid = Function(Dgb)        # Displacement VECTOR

        # Helper functions
        U1 = Function(V)
        U2 = Function(V)
        DF1 = Function(D)
        DF2 = Function(D)

        Uext = Extrapolation(V, self.params.r) # Velocity extrapolation
        Pext = Extrapolation(Q, self.params.s) # Pressure extrapolation
        phi = p - Pext
        phiext = Extrapolation(Q, self.params.r)
        DFext = Extrapolation(D, self.params.r)
        DFext1, DFext2 = Function(D), Function(D)
        w = Function(D)

        traction = Function(Dgb)                       # Fluid -> Solid
        ExternalForcing = problem.ExternalForcing

        # Get functions for data assimilation
        observations = problem.observations(spaces, t)
        controls = problem.controls(spaces)

        # Get initial conditions
        icu, icp, iceta = problem.initial_conditions(spaces, controls)
        ics = icu, icp
        # Set initial values
        assign_ics_split(U1, P, spaces, ics)
        Usolid.assign(iceta)

        Uext.update(U1)
        #Pext.update(P)
        #phiext.update(P - Pext)

        #####################
        # BOUNDARY CONDITIONS
        #####################
        # Make scheme-specific representation of bcs
        bcs = problem.boundary_conditions(spaces, U, P, t, None)
        bcu = make_velocity_bcs(problem, spaces, (bcs.u, bcs.p))
        bcp = make_pressure_bcs(problem, spaces, (bcs.u, bcs.p))
        # In our scheme the corrected velocity should satisfy kinematic bc on
        # FSI, i.e. should match the mesh velocity
        if not hasattr(problem.Epsilon, '__iter__'): problem.Epsilon = [problem.Epsilon]

        bcu_corr = [DirichletBC(V, w, problem.facet_domains, fsi_tag)
                    for fsi_tag in problem.Epsilon]
        # Boundary conditions of solid model are handled by its implem. Here we
        # just add the boundary(facet function) info
        bc_solid = [(value, problem.bmesh_boundaries, tag)
                    for (value, tag) in bcs.solid]
        # ALE matches solid on FSI and 'inherits' solid Dirichlet bcs
        bcs_ale = [DirichletBC(D, DF, problem.facet_domains, fsi_tag)
                   for fsi_tag in problem.Epsilon]

        bcs_ale.extend([DirichletBC(D, value, problem.facet_domains, tag)
                        for value, tag in bcs.solid])
        # Setup absorbing bcs - they are presribed on each outlet
        try:
            outlets = problem.outlet_domains
        except AttributeError:
            outlets = []
        outlets = set(outlets)
        # First check for inconsistency: ie not setting pressure by bcs on oulett
        assert len(outlets & set(bc[1] for bc in bcs.p)) == 0
        # Set each outlet 
        bcp_absorbing = []
        for outlet in outlets:
            # Note value here is AbsorbingStress, Dirichlet takes pointer to it
            # what that pointer points to is update in problem.update
            value = bcs.absorbing[outlet]
            bcp_absorbing.append(DirichletBC(Q, value, problem.facet_domains, outlet))

        #######
        # FORMS
        #######
        # Get fluid parameters
        mu = Constant(problem.params.mu)          # Fluid viscosity
        rho_f = Constant(problem.params.rho)      # Fluid density
        # Get solid params for coupling bc term
        rho_s = Constant(problem.params.rho_s)    # Solid density 
        h_s = Constant(problem.params.h)    # Thickness?  
        # Extrapolation
        r = self.params.r
        s = self.params.s

        nds = sum((ds(fsi_tag) for fsi_tag in problem.Epsilon[1:]), ds(problem.Epsilon[0]))

        F = Identity(dim) + grad(DF)
        J = det(F)

        # Tentative velocity, Eq. 30, mapped to reference mesh
        # Eq. 30.1
        a1 = rho_f/dt*inner(J*(u - U1), v)*dx()
        a1 += rho_f*inner(dot(grad(u)*(U1 - w), cofac(F)), v)*dx()
        a1 += inner(2*mu*Epsilon(u, F)*cofac(F), grad(v))*dx()

        # Eq. 30.3
        a1 += rho_s*h_s/dt*inner(u - (DF1 - DF2)/dt, v)*nds
        a1 -= rho_s*h_s*inner(par((DFext - 2*DFext1 + DFext2)/dt**2, n), v)*nds

        # Extrapolation cases of RHS of Eq. 30.3
        if r == 1:
            _F = Identity(dim) + grad(DF1)
            a1 -= 2*mu*inner(par(dot(Epsilon(U1, _F), cofac(_F)*n), n), v)*nds
        elif r == 2:
            _F = Identity(dim) + grad(DF1)
            a1 -= 2*2*mu*inner(par(dot(Epsilon(U1, _F), cofac(_F)*n), n), v)*nds

            _F = Identity(dim) + grad(DF2)
            a1 += 2*mu*inner(par(dot(Epsilon(U1, _F), cofac(_F)*n), n), v)*nds

        L1 = rhs(a1)
        a1 = lhs(a1)

        A1 = assemble(a1)
        b1 = assemble(L1)

        if tolerance < 0:
            solver_u_tent = create_solver('superlu_dist')
        else:
            solver_u_tent = create_solver("gmres", "additive_schwarz")
            solver_u_tent.parameters['relative_tolerance'] = tolerance
            solver_u_tent.parameters['absolute_tolerance'] = tolerance

        # Pressure, Eq 31, mapped to reference mesh (note phi=p - Pext)
        a2 = 1./J*inner(cofac(F)*grad(q), cofac(F)*grad(phi))*dx()
        a2 += rho_f/dt*inner(q, div(dot(U, cofac(F))))*dx()

        # Very unsure about these two terms (nds)
        a2 += J*rho_f/(rho_s*h_s)*(phi - phiext)*q*nds
        a2 -= J*rho_f/dt*dot(Uext - (DFext - DFext1)/dt, n)*nds

        L2 = rhs(a2)
        a2 = lhs(a2)

        A2 = assemble(a2, keep_diagonal=True)
        b2 = assemble(L2)

        if tolerance < 0:
            solver_p_corr = create_solver('superlu_dist')
        else:
            solver_p_corr = create_solver("bicgstab", "amg")
            solver_p_corr.parameters['relative_tolerance'] = tolerance
            solver_p_corr.parameters['absolute_tolerance'] = tolerance

        # Velocity correction (u^n = tilde(u)^n+tau/rho*grad phi^n)
        a3 = inner(J*u, v)*dx()
        a3 -= inner(J*U, v)*dx()
        a3 += dt/rho_f*inner(cofac(F)*grad(P - Pext), v)*dx()

        L3 = rhs(a3)
        a3 = lhs(a3)

        A3 = assemble(a3)
        b3 = assemble(L3)

        if tolerance < 0:
            solver_u_corr = create_solver('superlu_dist')
        else:
            solver_u_corr = create_solver("gmres", "additive_schwarz")
            solver_u_corr.parameters['relative_tolerance'] = tolerance
            solver_u_corr.parameters['absolute_tolerance'] = tolerance

        # Setup the solid model
        solid_step = problem.solid_model(solution=Usolid,
                                         traction=traction,
                                         n=nDgb,
                                         bcs=bc_solid,
                                         dt=dt,
                                         params=problem.params,
                                         tol=tolerance)

        # Mesh displacement 
        a5 = inner(grad(d), grad(e))*dx
        L5 = inner(Constant((0,)*dim), e)*dx
        ale_assembler = SystemAssembler(a5, L5, bcs_ale)
        A5, b5 = PETScMatrix(), PETScVector()
        ale_assembler.assemble(A5)
        # Since A5 is constant in simulation we can setup preconditioner now

        if tolerance < 0:
            ale_solver = create_solver('superlu_dist')
            ale_solver.set_operator(A5)
            ale_solver.parameters['reuse_factorization'] = True
        else:
            ale_solver = PETScKrylovSolver('cg', 'hypre_amg')
            ale_solver.set_operators(A5, A5)
            ale_solver.parameters['relative_tolerance'] = tolerance
            ale_solver.parameters['absolute_tolerance'] = tolerance

        for timestep in xrange(start_timestep + 1, len(timesteps)):
            t.assign(timesteps[timestep])

            # Update various functions: all bcs, all forcing
            problem.update(spaces, U, P, DF, t, timestep, bcs)
            timer.completed("problem update")

            ##########################
            # Solve tentative velocity
            ##########################
            assemble(a1, tensor=A1)
            assemble(L1, tensor=b1)

            for bc in bcu: bc.apply(A1, b1)

            solver_u_tent.solve(A1, U.vector(), b1)

            ###########################
            # Solve pressure correction
            ###########################
            assemble(a2, tensor=A2)
            assemble(L2, tensor=b2)
            # NOTE: apply absorbing pressure!
            for bc in bcp + bcp_absorbing: bc.apply(A2, b2)

            solver_p_corr.solve(A2, P.vector(), b2)

            ########################
            # Solve updated velocity
            ########################
            assemble(a3, tensor=A3)
            assemble(L3, tensor=b3)

            for bc in bcu + bcu_corr: bc.apply(A3, b3)

            solver_u_corr.solve(A3, U.vector(), b3)

            #############
            # Solve solid
            #############
            # Compute stress on the boundary = fluid stress + outside
            D_to_Dgb.map(dot(Sigma(mu, U, P, F), cofac(F)*n) + ExternalForcing*n, traction)
            # Update Usolid
            solid_step.solve()

            #########################
            # Solve mesh displacement
            #########################
            # Prolongate solid displacement from boundary to full mesh
            Dgb_to_D.map(Usolid, DF)
            # Assemble the rhs taking into account new bcs
            ale_assembler.assemble(b5)
            ale_solver.solve(DF.vector(), b5)

            # Rotate functions
            U1.assign(U)
            DF2.assign(DF1)
            DF1.assign(DF)
            w.assign(DF - DF1)
            w.vector()[:] *= 1./float(dt) # Mesh velocity

            # Update extrapolations
            Uext.update(U)
            phiext.update(P - Pext)
            Pext.update(P)
            DFext2.assign(DFext1)
            DFext1.assign(DFext)
            DFext.update(DF)
            
            yield ParamDict(spaces=spaces, observations=None, controls=None,
                            t=float(t), timestep=timestep, 
                            u=U, p=P, d=(Usolid, DF))